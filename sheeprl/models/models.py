"""
Adapted from: https://github.com/thu-ml/tianshou/blob/master/tianshou/utils/net/common.py
"""
from math import prod
from typing import Optional, Sequence, Type, Union, no_type_check

import torch.nn.functional as F
from torch import Tensor, nn

from sheeprl.utils.model import ArgsType, ModuleType, miniblock


class MLP(nn.Module):
    """Simple MLP backbone.

    Args:
        input_dims (Union[int, Sequence[int]]): dimensions of the input vector.
        output_dim (int, optional): dimension of the output vector. If set to 0, there
            is no final linear layer.
            Defaults to 0.
        hidden_sizes (Sequence[int], optional): shape of MLP passed in as a list, not including
            input_dims and output_dim.
        dropout_layer (Union[ModuleType, Sequence[ModuleType]], optional): which dropout layer to be used
            before activation (possibly before the normalization layer), e.g., ``nn.Dropout``.
            You can also pass a list of dropout modules with the same length
            of hidden_sizes to use different dropout modules in different layers.
            If None, then no dropout layer is used.
            Defaults to None.
        norm_layer (Union[ModuleType, Sequence[ModuleType]], optional): which normalization layer to be used
            before activation, e.g., ``nn.LayerNorm`` and ``nn.BatchNorm1d``.
            You can also pass a list of normalization modules with the same length
            of hidden_sizes to use different normalization modules in different layers.
            If None, then no normalization layer is used.
            Defaults to None.
        activation (Union[ModuleType, Sequence[ModuleType]], optional): which activation to use after each layer,
            can be both the same activation for all layers if a single ``nn.Module`` is passed, or different
            activations for different layers if a list is passed.
            Defaults to ``nn.ReLU``.
        device (Union[str, int, torch.device], optional): which device to create this model on.
            Defaults to "cpu".
        linear_layer (ModuleType, optional): which linear layer to use.
            Defaults to ``nn.Linear``.
        flatten_dim (int, optional): whether to flatten input data. The flatten dimension starts from 1.
            Defaults to True.
    """

    def __init__(
        self,
        input_dims: Union[int, Sequence[int]],
        output_dim: int = 0,
        hidden_sizes: Sequence[int] = (),
        dropout_layer: Optional[Union[ModuleType, Sequence[ModuleType]]] = None,
        dropout_args: Optional[ArgsType] = None,
        norm_layer: Optional[Union[ModuleType, Sequence[ModuleType]]] = None,
        norm_args: Optional[ArgsType] = None,
        activation: Optional[Union[ModuleType, Sequence[ModuleType]]] = nn.ReLU,
        act_args: Optional[ArgsType] = None,
        linear_layer: Type[nn.Linear] = nn.Linear,
        flatten_dim: Optional[int] = 1,
    ) -> None:
        super().__init__()
        if dropout_layer:
            if isinstance(dropout_layer, list):
                assert len(dropout_layer) == len(hidden_sizes)
                dropout_layer_list = dropout_layer
                if isinstance(dropout_args, list):
                    assert len(dropout_args) == len(hidden_sizes)
                    dropout_args_list = dropout_args
                else:
                    dropout_args_list = [dropout_args for _ in range(len(hidden_sizes))]
            else:
                dropout_layer_list = [dropout_layer for _ in range(len(hidden_sizes))]
                dropout_args_list = [dropout_args for _ in range(len(hidden_sizes))]
        else:
            dropout_layer_list = [None] * len(hidden_sizes)
            dropout_args_list = [None] * len(hidden_sizes)
        if norm_layer:
            if isinstance(norm_layer, list):
                assert len(norm_layer) == len(hidden_sizes)
                norm_layer_list = norm_layer
                if isinstance(norm_args, list):
                    assert len(norm_args) == len(hidden_sizes)
                    norm_args_list = norm_args
                else:
                    norm_args_list = [norm_args for _ in range(len(hidden_sizes))]
            else:
                norm_layer_list = [norm_layer for _ in range(len(hidden_sizes))]
                norm_args_list = [norm_args for _ in range(len(hidden_sizes))]
        else:
            norm_layer_list = [None] * len(hidden_sizes)
            norm_args_list = [None] * len(hidden_sizes)
        if activation:
            if isinstance(activation, list):
                assert len(activation) == len(hidden_sizes)
                activation_list = activation
                if isinstance(act_args, list):
                    assert len(act_args) == len(hidden_sizes)
                    act_args_list = act_args
                else:
                    act_args_list = [act_args for _ in range(len(hidden_sizes))]
            else:
                activation_list = [activation for _ in range(len(hidden_sizes))]
                act_args_list = [act_args for _ in range(len(hidden_sizes))]
        else:
            activation_list = [None] * len(hidden_sizes)
            act_args_list = [None] * len(hidden_sizes)
        if isinstance(input_dims, int):
            input_dims = [input_dims]
        hidden_sizes = [prod(input_dims)] + list(hidden_sizes)
        model = []
        for in_dim, out_dim, drop, drop_args, norm, norm_args, activ, act_args in zip(
            hidden_sizes[:-1],
            hidden_sizes[1:],
            dropout_layer_list,
            dropout_args_list,
            norm_layer_list,
            norm_args_list,
            activation_list,
            act_args_list,
        ):
            model += miniblock(in_dim, out_dim, drop, drop_args, norm, norm_args, activ, act_args, linear_layer)
        if output_dim > 0:
            model += [linear_layer(hidden_sizes[-1], output_dim)]
        self._output_dim = output_dim or hidden_sizes[-1]
        self._model = nn.Sequential(*model)
        self._flatten_dim = flatten_dim

    @property
    def model(self) -> nn.Module:
        return self._model

    @property
    def output_dim(self) -> int:
        return self._output_dim

    @property
    def flatten_dim(self) -> Optional[int]:
        return self._flatten_dim

    @no_type_check
    def forward(self, obs: Tensor) -> Tensor:
        if self.flatten_dim is not None:
            obs = obs.flatten(self.flatten_dim)
        return self.model(obs)


class NatureCNN(nn.Module):
    """CNN from DQN Nature paper: Mnih, Volodymyr, et al. "Human-level control through deep reinforcement learning."
    Nature 518.7540 (2015): 529-533.

    Args:
        in_channels (int): the input channels to the first convolutional layer
        features_dim (int): the features dimension in output from the last convolutional layer
    """

    def __init__(self, in_channels: int, features_dim: int):
        super().__init__()
        self.conv1 = nn.Conv2d(in_channels, 32, kernel_size=8, stride=4)
        self.conv2 = nn.Conv2d(32, 64, kernel_size=4, stride=2)
        self.conv3 = nn.Conv2d(64, 64, kernel_size=3, stride=1)
        self.fc = nn.Linear(7 * 7 * 64, features_dim)

    def forward(self, x: Tensor) -> Tensor:
        x = F.relu(self.conv1(x))
        x = F.relu(self.conv2(x))
        x = F.relu(self.conv3(x))
        x = F.relu(self.fc(x.flatten(1)))
        return x