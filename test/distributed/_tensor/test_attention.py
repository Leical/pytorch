# Copyright (c) Meta Platforms, Inc. and affiliates
# Owner(s): ["oncall: distributed"]

import torch
from torch.distributed._tensor import DeviceMesh, DTensor, Replicate, Shard
from torch.distributed._tensor.debug import CommDebugMode
from torch.distributed._tensor.experimental.attention import attention_parallel
from torch.testing._internal.common_utils import (
    instantiate_parametrized_tests,
    run_tests,
)
from torch.testing._internal.distributed._tensor.common_dtensor import (
    DTensorTestBase,
    NUM_DEVICES,
    with_comms,
)


c10d_functional = torch.ops.c10d_functional


class RingAttentionTest(DTensorTestBase):
    @with_comms
    def test_ring_attention(self):
        device_mesh = DeviceMesh(
            self.device_type,
            torch.arange(0, NUM_DEVICES),
        )
        dtype = torch.bfloat16
        bs = 8
        query_tokens = 8
        context_tokens = 16
        dim = 32
        nheads = 8
        query = torch.ones(
            (bs, nheads, query_tokens, dim),
            device=self.device_type,
            dtype=dtype,
            requires_grad=True,
        )
        key = torch.ones(
            (bs, nheads, context_tokens, dim), device=self.device_type, dtype=dtype
        )
        value = torch.ones(
            (bs, nheads, context_tokens, dim), device=self.device_type, dtype=dtype
        )

        query_placement = [Replicate()]
        query = DTensor.from_local(query, device_mesh, query_placement)
        self.assertEqual(query.shape, (bs, nheads, query_tokens, dim))

        context_placement = [Shard(2)]
        key = DTensor.from_local(key, device_mesh, context_placement)
        value = DTensor.from_local(value, device_mesh, context_placement)
        self.assertEqual(key.shape, (bs, nheads, context_tokens*self.world_size, dim))
        self.assertEqual(value.shape, (bs, nheads, context_tokens*self.world_size, dim))

        # local tensors

        out = torch.ops.aten._scaled_dot_product_flash_attention(query.full_tensor(), key.full_tensor(), value.full_tensor())[0]
        self.assertEqual(out.shape, (bs, nheads, query_tokens, dim))

        # nonparallel DTensor

        with CommDebugMode() as comm_mode:
            out = torch.ops.aten._scaled_dot_product_flash_attention(query, key, value)[0]
        self.assertDictEqual(
            comm_mode.get_comm_counts(),
            {
                #c10d_functional.all_to_all_single: self.world_size - 1,
            },
        )
        self.assertEqual(out.shape, (bs, nheads, query_tokens, dim))
        out = out.full_tensor()

        # parallel behavior

        with attention_parallel(), CommDebugMode() as comm_mode:
            out_parallel = torch.ops.aten._scaled_dot_product_flash_attention(
                query, key, value
            )[0]
        self.assertDictEqual(
            comm_mode.get_comm_counts(),
            {
                c10d_functional.all_to_all_single: self.world_size - 1,
            },
        )
        self.assertEqual(out_parallel._local_tensor.shape, (bs, nheads, query_tokens, dim))
        self.assertEqual(out_parallel.shape, (bs, nheads, query_tokens, dim))

        # TODO: verify numerical accuracy
        self.assertEqual(out_parallel.to_local(), out)

        # TODO backwards
        # out[0].sum().backward()


instantiate_parametrized_tests(DTensorTestBase)

if __name__ == "__main__":
    run_tests()
