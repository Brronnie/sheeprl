import pytest
import torch
from tensordict import TensorDict

from sheeprl.data.buffers import ReplayBuffer


def test_replay_buffer_wrong_buffer_size():
    with pytest.raises(ValueError):
        ReplayBuffer(-1)


def test_replay_buffer_wrong_n_envs():
    with pytest.raises(ValueError):
        ReplayBuffer(1, -1)


def test_replay_buffer_add_tds():
    buf_size = 5
    n_envs = 1
    rb = ReplayBuffer(buf_size, n_envs)
    td1 = TensorDict({"t": torch.rand(2, 1, 1)}, batch_size=[2, n_envs])
    td2 = TensorDict({"t": torch.rand(2, 1, 1)}, batch_size=[2, n_envs])
    td3 = TensorDict({"t": torch.rand(3, 1, 1)}, batch_size=[3, n_envs])
    rb.add(td1)
    rb.add(td2)
    rb.add(td3)
    assert rb.full
    assert rb["t"][0] == td3["t"][-2]
    assert rb["t"][1] == td3["t"][-1]
    torch.testing.assert_close(rb["t"][2:4], td2["t"])


def test_replay_buffer_add_single_td():
    buf_size = 5
    n_envs = 1
    rb = ReplayBuffer(buf_size, n_envs)
    td1 = TensorDict({"t": torch.rand(6, 1, 1)}, batch_size=[6, n_envs])
    rb.add(td1)
    assert rb.full
    assert rb["t"][0] == td1["t"][-1]


def test_replay_buffer_sample():
    buf_size = 5
    n_envs = 1
    rb = ReplayBuffer(buf_size, n_envs)
    td1 = TensorDict({"t": torch.rand(6, 1, 1)}, batch_size=[6, n_envs])
    rb.add(td1)
    s = rb.sample(4)
    assert s.shape == torch.Size([4, 1])


def test_replay_buffer_sample_full():
    buf_size = 5
    n_envs = 1
    rb = ReplayBuffer(buf_size, n_envs)
    td1 = TensorDict({"t": torch.rand(6, 1, 1)}, batch_size=[6, n_envs])
    rb.add(td1)
    s = rb.sample(6)
    assert s.shape == torch.Size([6, 1])


def test_replay_buffer_sample_one_element():
    buf_size = 1
    n_envs = 1
    rb = ReplayBuffer(buf_size, n_envs)
    td1 = TensorDict({"t": torch.rand(1, 1, 1)}, batch_size=[1, n_envs])
    rb.add(td1)
    sample = rb.sample(1)
    assert rb.full
    assert sample["t"] == td1["t"]
    with pytest.raises(RuntimeError):
        rb.sample(1, sample_next_obs=True)