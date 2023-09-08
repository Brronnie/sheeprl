import copy
import os
import time
import warnings
from math import prod
from typing import Optional, Union

import gymnasium as gym
import hydra
import numpy as np
import torch
import torch.nn.functional as F
from lightning.fabric import Fabric
from lightning.fabric.accelerators import CUDAAccelerator, TPUAccelerator
from lightning.fabric.fabric import _is_using_cli
from lightning.fabric.plugins.collectives.collective import CollectibleGroup
from lightning.fabric.strategies import DDPStrategy, SingleDeviceStrategy
from lightning.fabric.wrappers import _FabricModule
from omegaconf import DictConfig, OmegaConf
from tensordict import TensorDict, make_tensordict
from tensordict.tensordict import TensorDictBase
from torch.optim import Optimizer
from torch.utils.data.distributed import DistributedSampler
from torch.utils.data.sampler import BatchSampler
from torchmetrics import MeanMetric

from sheeprl.algos.sac.loss import critic_loss, entropy_loss, policy_loss
from sheeprl.algos.sac_ae.agent import (
    CNNDecoder,
    CNNEncoder,
    MLPDecoder,
    MLPEncoder,
    SACAEAgent,
    SACAEContinuousActor,
    SACAECritic,
    SACAEQFunction,
)
from sheeprl.algos.sac_ae.utils import preprocess_obs, test_sac_ae
from sheeprl.data.buffers import ReplayBuffer
from sheeprl.models.models import MultiDecoder, MultiEncoder
from sheeprl.utils.callback import CheckpointCallback
from sheeprl.utils.env import make_dict_env
from sheeprl.utils.logger import create_tensorboard_logger
from sheeprl.utils.metric import MetricAggregator
from sheeprl.utils.registry import register_algorithm


def train(
    fabric: Fabric,
    agent: SACAEAgent,
    encoder: Union[MultiEncoder, _FabricModule],
    decoder: Union[MultiDecoder, _FabricModule],
    actor_optimizer: Optimizer,
    qf_optimizer: Optimizer,
    alpha_optimizer: Optimizer,
    encoder_optimizer: Optimizer,
    decoder_optimizer: Optimizer,
    data: TensorDictBase,
    aggregator: MetricAggregator,
    global_step: int,
    cfg: DictConfig,
    group: Optional[CollectibleGroup] = None,
):
    data = data.to(fabric.device)
    normalized_obs = {}
    normalized_next_obs = {}
    for k in cfg.cnn_keys.encoder + cfg.mlp_keys.encoder:
        if k in cfg.cnn_keys.encoder:
            normalized_obs[k] = data[k] / 255.0
            normalized_next_obs[k] = data[f"next_{k}"] / 255.0
        else:
            normalized_obs[k] = data[k]
            normalized_next_obs[k] = data[f"next_{k}"]

    # Update the soft-critic
    next_target_qf_value = agent.get_next_target_q_values(
        normalized_next_obs, data["rewards"], data["dones"], cfg.algo.gamma
    )
    qf_values = agent.get_q_values(normalized_obs, data["actions"])
    qf_loss = critic_loss(qf_values, next_target_qf_value, agent.num_critics)
    qf_optimizer.zero_grad(set_to_none=True)
    fabric.backward(qf_loss)
    qf_optimizer.step()
    aggregator.update("Loss/value_loss", qf_loss)

    # Update the target networks with EMA
    if global_step % cfg.algo.critic.target_network_frequency == 0:
        agent.critic_target_ema()
        agent.critic_encoder_target_ema()

    # Update the actor
    if global_step % cfg.algo.actor.network_frequency == 0:
        actions, logprobs = agent.get_actions_and_log_probs(normalized_obs, detach_encoder_features=True)
        qf_values = agent.get_q_values(normalized_obs, actions, detach_encoder_features=True)
        min_qf_values = torch.min(qf_values, dim=-1, keepdim=True)[0]
        actor_loss = policy_loss(agent.alpha, logprobs, min_qf_values)
        actor_optimizer.zero_grad(set_to_none=True)
        fabric.backward(actor_loss)
        actor_optimizer.step()
        aggregator.update("Loss/policy_loss", actor_loss)

        # Update the entropy value
        alpha_loss = entropy_loss(agent.log_alpha, logprobs.detach(), agent.target_entropy)
        alpha_optimizer.zero_grad(set_to_none=True)
        fabric.backward(alpha_loss)
        agent.log_alpha.grad = fabric.all_reduce(agent.log_alpha.grad, group=group)
        alpha_optimizer.step()
        aggregator.update("Loss/alpha_loss", alpha_loss)

    # Update the decoder
    if global_step % cfg.algo.decoder.update_freq == 0:
        hidden = encoder(normalized_obs)
        reconstruction = decoder(hidden)
        reconstruction_loss = 0
        for k in cfg.cnn_keys.decoder + cfg.mlp_keys.decoder:
            target = preprocess_obs(data[k], bits=5) if k in cfg.cnn_keys.decoder else data[k]
            reconstruction_loss += (
                F.mse_loss(target, reconstruction[k])  # Reconstruction
                + cfg.algo.decoder.l2_lambda * (0.5 * hidden.pow(2).sum(1)).mean()  # L2 penalty on the hidden state
            )
        encoder_optimizer.zero_grad(set_to_none=True)
        decoder_optimizer.zero_grad(set_to_none=True)
        fabric.backward(reconstruction_loss)
        encoder_optimizer.step()
        decoder_optimizer.step()
        aggregator.update("Loss/reconstruction_loss", reconstruction_loss)


@register_algorithm()
@hydra.main(version_base=None, config_path="../../configs", config_name="config")
def main(cfg: DictConfig):
    if "minedojo" in cfg.env.env._target_.lower():
        raise ValueError(
            "MineDojo is not currently supported by SAC-AE agent, since it does not take "
            "into consideration the action masks provided by the environment, but needed "
            "in order to play correctly the game. "
            "As an alternative you can use one of the Dreamers' agents."
        )

    # These arguments cannot be changed
    cfg.env.screen_size = 64

    # Initialize Fabric
    devices = os.environ.get("LT_DEVICES", None)
    strategy = os.environ.get("LT_STRATEGY", None)
    is_tpu_available = TPUAccelerator.is_available()
    if strategy is not None:
        warnings.warn(
            "You are running the SAC-AE algorithm through the Lightning CLI and you have specified a strategy: "
            f"`lightning run model --strategy={strategy}`. This algorithm is run with the "
            "`lightning.fabric.strategies.DDPStrategy` strategy, unless a TPU is available."
        )
        os.environ.pop("LT_STRATEGY")
    if is_tpu_available:
        strategy = "auto"
    else:
        strategy = DDPStrategy(find_unused_parameters=True)
        if devices == "1":
            strategy = SingleDeviceStrategy(device="cuda:0" if CUDAAccelerator.is_available() else "cpu")
    fabric = Fabric(strategy=strategy, callbacks=[CheckpointCallback()])
    if not _is_using_cli():
        fabric.launch()
    rank = fabric.global_rank
    device = fabric.device
    fabric.seed_everything(cfg.seed)
    torch.backends.cudnn.deterministic = cfg.torch_deterministic

    # Create TensorBoardLogger. This will create the logger only on the
    # rank-0 process
    logger, log_dir = create_tensorboard_logger(fabric, cfg, "sac_ae")
    if fabric.is_global_zero:
        fabric._loggers = [logger]
        fabric.logger.log_hyperparams(OmegaConf.to_container(cfg, resolve=True))

    # Environment setup
    vectorized_env = gym.vector.SyncVectorEnv if cfg.env.sync_env else gym.vector.AsyncVectorEnv
    envs = vectorized_env(
        [
            make_dict_env(
                cfg,
                cfg.seed + rank * cfg.num_envs + i,
                rank * cfg.num_envs,
                logger.log_dir if rank == 0 else None,
                "train",
                vector_env_idx=i,
            )
            for i in range(cfg.num_envs)
        ]
    )
    observation_space = envs.single_observation_space

    if not isinstance(observation_space, gym.spaces.Dict):
        raise RuntimeError(f"Unexpected observation type, should be of type Dict, got: {observation_space}")
    if cfg.cnn_keys.encoder == [] and cfg.mlp_keys.encoder == []:
        raise RuntimeError(
            "You should specify at least one CNN keys or MLP keys from the cli: `--cnn_keys rgb` "
            "or `--mlp_keys state` "
        )
    if (
        len(set(cfg.cnn_keys.encoder).intersection(set(cfg.cnn_keys.decoder))) == 0
        and len(set(cfg.mlp_keys.encoder).intersection(set(cfg.mlp_keys.decoder))) == 0
    ):
        raise RuntimeError("The CNN keys or the MLP keys of the encoder and decoder must not be disjoint")
    if len(set(cfg.cnn_keys.decoder) - set(cfg.cnn_keys.encoder)) > 0:
        raise RuntimeError(
            "The CNN keys of the decoder must be contained in the encoder ones. "
            f"Those keys are decoded without being encoded: {list(set(cfg.cnn_keys.decoder))}"
        )
    if len(set(cfg.mlp_keys.decoder) - set(cfg.mlp_keys.encoder)) > 0:
        raise RuntimeError(
            "The MLP keys of the decoder must be contained in the encoder ones. "
            f"Those keys are decoded without being encoded: {list(set(cfg.mlp_keys.decoder))}"
        )
    fabric.print("Encoder CNN keys:", cfg.cnn_keys.encoder)
    fabric.print("Encoder MLP keys:", cfg.mlp_keys.encoder)
    fabric.print("Decoder CNN keys:", cfg.cnn_keys.decoder)
    fabric.print("Decoder MLP keys:", cfg.mlp_keys.decoder)

    # Define the agent and the optimizer and setup them with Fabric
    act_dim = prod(envs.single_action_space.shape)
    target_entropy = -act_dim

    # Define the encoder and decoder and setup them with fabric.
    # Then we will set the critic encoder and actor decoder as the unwrapped encoder module:
    # we do not need it wrapped with the strategy inside actor and critic
    cnn_channels = [prod(envs.single_observation_space[k].shape[:-2]) for k in cfg.cnn_keys.encoder]
    mlp_dims = [envs.single_observation_space[k].shape[0] for k in cfg.mlp_keys.encoder]
    cnn_encoder = (
        CNNEncoder(
            in_channels=sum(cnn_channels),
            features_dim=cfg.algo.encoder.features_dim,
            keys=cfg.cnn_keys.encoder,
            screen_size=cfg.env.screen_size,
            cnn_channels_multiplier=cfg.algo.encoder.cnn_channels_multiplier,
        )
        if cfg.cnn_keys.encoder is not None and len(cfg.cnn_keys.encoder) > 0
        else None
    )
    mlp_encoder = (
        MLPEncoder(
            sum(mlp_dims),
            cfg.mlp_keys.encoder,
            cfg.algo.encoder.dense_units,
            cfg.algo.encoder.mlp_layers,
            eval(cfg.algo.encoder.dense_act),
            cfg.algo.encoder.layer_norm,
        )
        if cfg.mlp_keys.encoder is not None and len(cfg.mlp_keys.encoder) > 0
        else None
    )
    encoder = MultiEncoder(cnn_encoder, mlp_encoder)
    cnn_decoder = (
        CNNDecoder(
            cnn_encoder.conv_output_shape,
            features_dim=encoder.output_dim,
            keys=cfg.cnn_keys.decoder,
            channels=cnn_channels,
            screen_size=cfg.env.screen_size,
            cnn_channels_multiplier=cfg.algo.decoder.cnn_channels_multiplier,
        )
        if cfg.cnn_keys.decoder is not None and len(cfg.cnn_keys.decoder) > 0
        else None
    )
    mlp_decoder = (
        MLPDecoder(
            encoder.output_dim,
            mlp_dims,
            cfg.mlp_keys.decoder,
            cfg.algo.decoder.dense_units,
            cfg.algo.decoder.mlp_layers,
            eval(cfg.algo.decoder.dense_act),
            cfg.algo.decoder.layer_norm,
        )
        if cfg.mlp_keys.decoder is not None and len(cfg.mlp_keys.decoder) > 0
        else None
    )
    decoder = MultiDecoder(cnn_decoder, mlp_decoder)
    encoder = fabric.setup_module(encoder)
    decoder = fabric.setup_module(decoder)

    # Setup actor and critic. Those will initialize with orthogonal weights
    # both the actor and critic
    actor = SACAEContinuousActor(
        encoder=copy.deepcopy(encoder.module),
        action_dim=act_dim,
        hidden_size=cfg.algo.actor.hidden_size,
        action_low=envs.single_action_space.low,
        action_high=envs.single_action_space.high,
    )
    qfs = [
        SACAEQFunction(
            input_dim=encoder.output_dim, action_dim=act_dim, hidden_size=cfg.algo.critic.hidden_size, output_dim=1
        )
        for _ in range(cfg.algo.critic.n)
    ]
    critic = SACAECritic(encoder=encoder.module, qfs=qfs)
    actor = fabric.setup_module(actor)
    critic = fabric.setup_module(critic)

    # The agent will tied convolutional and linear weights between the encoder actor and critic
    agent = SACAEAgent(
        actor,
        critic,
        target_entropy,
        alpha=cfg.algo.alpha.alpha,
        tau=cfg.algo.tau,
        encoder_tau=cfg.algo.encoder.tau,
        device=fabric.device,
    )

    # Optimizers

    qf_optimizer, actor_optimizer, alpha_optimizer, encoder_optimizer, decoder_optimizer = fabric.setup_optimizers(
        hydra.utils.instantiate(cfg.algo.critic.optimizer, params=agent.critic.parameters()),
        hydra.utils.instantiate(cfg.algo.actor.optimizer, params=agent.actor.parameters()),
        hydra.utils.instantiate(cfg.algo.alpha.optimizer, params=[agent.log_alpha]),
        hydra.utils.instantiate(cfg.algo.encoder.optimizer, params=encoder.parameters()),
        hydra.utils.instantiate(cfg.algo.decoder.optimizer, params=decoder.parameters()),
    )

    # Metrics
    with device:
        aggregator = MetricAggregator(
            {
                "Rewards/rew_avg": MeanMetric(),
                "Game/ep_len_avg": MeanMetric(),
                "Time/step_per_second": MeanMetric(),
                "Loss/value_loss": MeanMetric(),
                "Loss/policy_loss": MeanMetric(),
                "Loss/alpha_loss": MeanMetric(),
                "Loss/reconstruction_loss": MeanMetric(),
            }
        )

    # Local data
    buffer_size = cfg.buffer.size // int(cfg.num_envs * fabric.world_size) if not cfg.dry_run else 1
    rb = ReplayBuffer(
        buffer_size,
        cfg.num_envs,
        device=fabric.device if cfg.buffer.memmap else "cpu",
        memmap=cfg.buffer.memmap,
        memmap_dir=os.path.join(log_dir, "memmap_buffer", f"rank_{fabric.global_rank}"),
        obs_keys=cfg.cnn_keys.encoder + cfg.mlp_keys.encoder,
    )
    step_data = TensorDict({}, batch_size=[cfg.num_envs], device=fabric.device if cfg.buffer.memmap else "cpu")

    # Global variables
    start_time = time.time()
    num_updates = int(cfg.total_steps // (cfg.num_envs * fabric.world_size)) if not cfg.dry_run else 1
    learning_starts = cfg.learning_starts // int(cfg.num_envs * fabric.world_size) if not cfg.dry_run else 0

    # Get the first environment observation and start the optimization
    o = envs.reset(seed=cfg.seed)[0]  # [N_envs, N_obs]
    obs = {}
    for k in o.keys():
        if k in cfg.cnn_keys.encoder + cfg.mlp_keys.encoder:
            torch_obs = torch.from_numpy(o[k]).to(fabric.device)
            if k in cfg.cnn_keys.encoder:
                torch_obs = torch_obs.view(cfg.num_envs, -1, *torch_obs.shape[-2:])
            if k in cfg.mlp_keys.encoder:
                torch_obs = torch_obs.float()
            obs[k] = torch_obs

    for global_step in range(1, num_updates + 1):
        if global_step < learning_starts:
            actions = envs.action_space.sample()
        else:
            with torch.no_grad():
                normalized_obs = {k: v / 255 if k in cfg.cnn_keys.encoder else v for k, v in obs.items()}
                actions, _ = actor.module(normalized_obs)
                actions = actions.cpu().numpy()
        o, rewards, dones, truncated, infos = envs.step(actions)
        dones = np.logical_or(dones, truncated)

        if "final_info" in infos:
            for i, agent_final_info in enumerate(infos["final_info"]):
                if agent_final_info is not None and "episode" in agent_final_info:
                    fabric.print(
                        f"Rank-0: global_step={global_step}, reward_env_{i}={agent_final_info['episode']['r'][0]}"
                    )
                    aggregator.update("Rewards/rew_avg", agent_final_info["episode"]["r"][0])
                    aggregator.update("Game/ep_len_avg", agent_final_info["episode"]["l"][0])

        # Save the real next observation
        real_next_obs = copy.deepcopy(o)
        if "final_observation" in infos:
            for idx, final_obs in enumerate(infos["final_observation"]):
                if final_obs is not None:
                    for k, v in final_obs.items():
                        real_next_obs[k][idx] = v

        next_obs = {}
        for k in real_next_obs.keys():
            next_obs[k] = torch.from_numpy(o[k]).to(fabric.device)
            if k in cfg.cnn_keys.encoder:
                next_obs[k] = next_obs[k].view(cfg.num_envs, -1, *next_obs[k].shape[-2:])
            if k in cfg.mlp_keys.encoder:
                next_obs[k] = next_obs[k].float()

            step_data[k] = obs[k]
            if not cfg.buffer.sample_next_obs:
                step_data[f"next_{k}"] = torch.from_numpy(real_next_obs[k]).to(fabric.device)
                if k in cfg.cnn_keys.encoder:
                    step_data[f"next_{k}"] = step_data[f"next_{k}"].view(
                        cfg.num_envs, -1, *step_data[f"next_{k}"].shape[-2:]
                    )
                if k in cfg.mlp_keys.encoder:
                    step_data[f"next_{k}"] = step_data[f"next_{k}"].float()
        actions = torch.from_numpy(actions).view(cfg.num_envs, -1).float().to(fabric.device)
        rewards = torch.from_numpy(rewards).view(cfg.num_envs, -1).float().to(fabric.device)
        dones = torch.from_numpy(dones).view(cfg.num_envs, -1).float().to(fabric.device)

        step_data["dones"] = dones
        step_data["actions"] = actions
        step_data["rewards"] = rewards
        rb.add(step_data.unsqueeze(0))

        # next_obs becomes the new obs
        obs = next_obs

        # Train the agent
        if global_step >= learning_starts - 1:
            training_steps = learning_starts if global_step == learning_starts - 1 else 1
            for _ in range(training_steps):
                # We sample one time to reduce the communications between processes
                sample = rb.sample(
                    cfg.gradient_steps * cfg.per_rank_batch_size, sample_next_obs=cfg.buffer.sample_next_obs
                )  # [G*B, 1]
                gathered_data = fabric.all_gather(sample.to_dict())  # [G*B, World, 1]
                gathered_data = make_tensordict(gathered_data).view(-1)  # [G*B*World]
                if fabric.world_size > 1:
                    dist_sampler: DistributedSampler = DistributedSampler(
                        range(len(gathered_data)),
                        num_replicas=fabric.world_size,
                        rank=fabric.global_rank,
                        shuffle=True,
                        seed=cfg.seed,
                        drop_last=False,
                    )
                    sampler: BatchSampler = BatchSampler(
                        sampler=dist_sampler, batch_size=cfg.per_rank_batch_size, drop_last=False
                    )
                else:
                    sampler = BatchSampler(
                        sampler=range(len(gathered_data)), batch_size=cfg.per_rank_batch_size, drop_last=False
                    )
                for batch_idxes in sampler:
                    train(
                        fabric,
                        agent,
                        encoder,
                        decoder,
                        actor_optimizer,
                        qf_optimizer,
                        alpha_optimizer,
                        encoder_optimizer,
                        decoder_optimizer,
                        gathered_data[batch_idxes],
                        aggregator,
                        global_step,
                        cfg,
                    )
        aggregator.update("Time/step_per_second", int(global_step / (time.time() - start_time)))
        fabric.log_dict(aggregator.compute(), global_step)
        aggregator.reset()

        # Checkpoint model
        if (cfg.checkpoint_every > 0 and global_step % cfg.checkpoint_every == 0) or cfg.dry_run:
            state = {
                "agent": agent.state_dict(),
                "encoder": encoder.state_dict(),
                "decoder": decoder.state_dict(),
                "qf_optimizer": qf_optimizer.state_dict(),
                "actor_optimizer": actor_optimizer.state_dict(),
                "alpha_optimizer": alpha_optimizer.state_dict(),
                "encoder_optimizer": encoder_optimizer.state_dict(),
                "decoder_optimizer": decoder_optimizer.state_dict(),
                "global_step": global_step * fabric.world_size,
                "batch_size": cfg.per_rank_batch_size * fabric.world_size,
            }
            ckpt_path = os.path.join(log_dir, f"checkpoint/ckpt_{global_step}_{fabric.global_rank}.ckpt")
            fabric.call(
                "on_checkpoint_coupled",
                fabric=fabric,
                ckpt_path=ckpt_path,
                state=state,
                replay_buffer=rb if cfg.buffer.checkpoint else None,
            )

    envs.close()
    if fabric.is_global_zero:
        test_env = make_dict_env(cfg, cfg.seed, 0, fabric.logger.log_dir, "test", vector_env_idx=0)()
        test_sac_ae(actor.module, test_env, fabric, cfg)


if __name__ == "__main__":
    main()