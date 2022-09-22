import contextlib
import warnings
from dataclasses import dataclass
from enum import auto, Enum
from itertools import accumulate, chain
from typing import (
    Any,
    cast,
    Callable,
    Dict,
    Generator,
    Iterator,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

import torch
import torch.distributed as dist
import torch.nn as nn
import torch.nn.functional as F
from torch import Tensor

from ._utils import (
    _alloc_storage,
    _free_storage,
    _same_storage,
    _set_fsdp_flattened,
    p_assert,
)


__all__ = [
    "FlatParameter",
    "FlatParamHandle",
    "FlatParamShardMetadata",
    "ParamInfo",
    "SharedParamInfo",
    "HandleConfig",
    "HandleShardingStrategy",
    "HandleTrainingState",
]


class TensorFlattener:
    """
    This enables a pre-flatten and post-unflatten transform to be applied per
    parameter. To do so, use :func:`_set_tensor_flattener` to set a custom
    :class:`TensorFlattener` that implements the two transforms.
    """

    def pre_flatten_transform(
        self, tensor: torch.Tensor
    ) -> Tuple[Optional[Any], torch.Tensor]:
        # E.g. Converting `ShardedTensor` to local tensor
        ...

    def post_unflatten_transform(
        self, tensor: torch.Tensor, param_extension: Any
    ) -> torch.Tensor:
        # E.g. Converting local tensor to `ShardedTensor`
        ...


_flattener: Optional[TensorFlattener] = None


def _set_tensor_flattener(flattener: TensorFlattener) -> None:
    global _flattener
    _flattener = flattener


class ParamInfo(NamedTuple):
    """Information for an original module parameter."""

    param_name: str  # unprefixed
    module: nn.Module
    module_name: str


class SharedParamInfo(NamedTuple):
    """
    Additional information for a shared parameter.

    For each shared parameter, we designate one module and its parameter
    variable to be the primary owner, determined as the first one encountered
    in the parameter walk. These are prefixed with "prim". The primary module
    and parameter do not have their own :class:`SharedParamInfo` instance.
    """

    param_name: str  # unprefixed
    module: nn.Module
    module_name: str
    prim_param_name: str  # unprefixed
    prim_module: nn.Module
    prim_module_name: str


class FlatParamShardMetadata(NamedTuple):
    """
    This holds metadata specific to this rank's shard of the flattened
    parameter.

    Attributes:
        param_names (Tuple[str, ...]): Prefixed parameter names of this rank's
            shard of the parameters; see :class:`FlatParameter`.
        param_shapes (Tuple[torch.Size, ...]): Parameter shapes of this rank's
            shard of the parameters; see :class:`FlatParameter`.
        param_numels (Tuple[int, ...]): Parameter numels of this rank's shard
            of the parameters; see :class:`FlatParameter`.
        param_offsets (Tuple[Tuple[int, int], ...]): [start, end] offsets (in
            units of numels) giving this rank's part of each flattened
            original module parameter.
    """

    param_names: Tuple[str, ...]
    param_shapes: Tuple[torch.Size, ...]
    param_numels: Tuple[int, ...]
    param_offsets: Tuple[Tuple[int, int], ...]


# TODO (awgu): Prefix these with "Handle" for now to avoid circular imports and
# inadvertent misuses; coalesce with those in fully_sharded_data_parallel.py
# later
class HandleShardingStrategy(Enum):
    FULL_SHARD = auto()
    SHARD_GRAD_OP = auto()
    NO_SHARD = auto()


class HandleTrainingState(Enum):
    IDLE = auto()
    FORWARD = auto()
    BACKWARD_PRE = auto()
    BACKWARD_POST = auto()
    SUMMON_FULL_PARAMS = auto()


@dataclass
class HandleConfig:
    sharding_strategy: HandleShardingStrategy
    offload_params: bool
    param_dtype: Optional[torch.dtype]
    reduce_dtype: Optional[torch.dtype]
    keep_low_precision_grads: Optional[bool] = False


class FlatParameter(nn.Parameter):
    """
    This is the flattened parameter used by :class:`FullyShardedDataParallel`.
    It is comprised of one or more original parameters, which are flattened
    and concatenated to construct the flattened parameter.

    Under the current design, this parameter logically represents both the
    unsharded and sharded flattened parameter, and its data changes storages
    dynamically.
        - In the :class:`FullyShardedDataParallel` constructor, the parameter
        is initialized as unsharded and then sharded in-place.
        - At runtime, the parameter is lazily (re)-initialized. The sharded
        parameter data is saved in ``self._local_shard``, and a new ``Tensor``
        ``self._full_param_padded`` is created, which is the all-gather
        destination and owns the unsharded parameter storage thereafter. (See
        :meth:`FullyShardedDataParallel._init_param_attributes`.)
        - Throughout runtime, the parameter data changes storages as needed,
        e.g. to the sharded flattened parameter, reduced-precision sharded
        flattened parameter, or the unsharded flattened parameter.

    Attributes:
        _unpadded_unsharded_size (torch.Size): Unsharded flattened parameter's
            size without padding.
        _padded_unsharded_size (torch.Size): Unsharded flattened parameter's
            size with padding. This is only set for sharded strategies since
            they require padding for the all-gather.
        _sharded_size (torch.Size): Sharded flattened parameter's size with
            padding. This is also set for ``NO_SHARD``, in which case it is the
            same as the unsharded sizes. (We omit "padded" because there is no
            analogous unpadded one.)

        _param_infos (Tuple[ParamInfo, ...]): Each parameter's parameter info
            entry; see :class:`ParamInfo`.
        _numels (Tuple[int, ...]): Each parameter's numel.
        _shapes (Tuple[torch.Size, ...]): Each parameter's shape.
        _prefixed_param_names (Tuple[str, ...]): Each parameter's name prefixed
            with the parent module names starting from the module passed to
            construct this flattened parameter via :class:`FlatParamHandle`;
            the prefixed names are guaranteed to be unique within the subtree
            rooted in that module.
        _num_params (int): Number of original parameters flattened into this
            flattened parameter; this is the length of ``_param_infos``,
            ``_numels``, ``_shapes``, and ``_prefixed_param_names``.
        _shared_param_infos (Tuple[SharedParamInfo, ...]): Shared parameter
            info entries; see :class:`SharedParamInfo`.
        _param_extensions (Tuple[Optional[Any], ...]): Parameter extensions
            (i.e. some per-parameter state) used for the tensor flattener to
            customize pre-flatten and post-unflatten behavior.

        _shard_param_offsets (List[Tuple[int, int])): [start, end] offsets (in
            units of numel) giving this rank's part of each flattened original
            module parameter; for any parameter ``p`` that is not sharded
            across ranks, this will be [0, ``p.numel()``-1].
        _shard_indices (Tuple[int, int]): [start, end] indices (in units of
            parameters) for this rank's shard of the original model parameters,
            where the parameters follow the order in which they were originally
            flattened; this indexes appropriately into any data structure that
            follows the flattening order (e.g. ``_param_infos``, ``_numels``,
            etc.).
        _shard_numel_padded (int): Numel padded for this rank's sharded
            flattened parameter.

        _local_shard (Tensor): Sharded flattened parameter with padding if
            using a sharded strategy. If using ``NO_SHARD``, then this is the
            unpadded unsharded flattened parameter, and there is no notion of a
            sharded flattened parameter or padded unsharded flattened
            parameter.
        _full_param_padded (Tensor): Unsharded flattened parameter with
            padding. This is not defined for ``NO_SHARD``. When using mixed
            precision for parameters, this has the low precision.
        _full_prec_full_param_padded (Tensor): Full precision unsharded
            flattened parameter with padding. This is used for unsharding
            outside of computation when using mixed precision for parameters.
            This is never defined for ``NO_SHARD``.
        _post_backward_hook_state (Tuple[AccumulateGrad, RemovableHandle]):
            Flattened parameter's :class:`AccumulateGrad` object and
            post-backward hook handle.
        _mp_shard (Tensor): Low precision sharded flattened parameter with
            padding. This is only defined when parameter mixed precision is
            enabled. For ``NO_SHARD``, this is used for computation.
        _cpu_grad (Tensor): Sharded gradient with padding stored on CPU.
            This is only defined when offloading parameters is enabled.
        _saved_grad_shard (Tensor): Sharded gradient with padding from previous
            iterations for gradient accumulation without :meth:`no_sync`.
    """

    def _init_metadata(
        self,
        param_infos: List[ParamInfo],
        numels: List[int],
        shapes: List[torch.Size],
        prefixed_param_names: List[str],
        shared_param_infos: List[SharedParamInfo],
        param_extensions: List[Any],
        params: Optional[List[nn.Parameter]],
        shared_params: Optional[List[nn.Parameter]],
    ) -> None:
        """
        Initializes attributes holding metadata about the original parameters
        comprising the flattened parameter.

        We expose this method separate from the constructor to keep the
        constructor only responsible for the flattened parameter's tensor data.
        This method should only be called once per model, while the constructor
        may be called multiple times, e.g. when reloading from a checkpoint, in
        which case only the tensor data needs to be passed to the constructor.
        Since :meth:`load_state_dict` is implemented via :meth:`copy_`, the
        metadata is correctly assumed to be unchanged.

        Args:
            See the Attributes in the class docstring.
        """
        assert len(param_infos) == len(numels)
        assert len(param_infos) == len(shapes)
        assert len(param_infos) == len(prefixed_param_names)
        assert len(param_infos) == len(param_extensions)
        self._num_params = len(param_infos)
        self._param_infos = tuple(param_infos)
        self._numels = tuple(numels)
        self._shapes = tuple(shapes)
        self._prefixed_param_names = tuple(prefixed_param_names)
        self._shared_param_infos = tuple(shared_param_infos)
        self._param_extensions = tuple(param_extensions)
        assert (params is None) == (shared_params is None)
        if params is not None:
            assert shared_params is not None
            self._params: Optional[List[nn.Parameter]] = params
            self._shared_params: Optional[List[nn.Parameter]] = shared_params
            # Mark the original parameters to avoid flattening them into
            # another `FlatParameter` during recursive construction
            for param in chain(self._params, self._shared_params):
                _set_fsdp_flattened(param)
        else:
            self._params = None
            self._shared_params = None
        self._unpadded_unsharded_size = self.size()
        _set_fsdp_flattened(self)


class FlatParamHandle:
    """
    This handle manages a flattened parameter (:class:`FlatParameter`). This
    includes sharding and view management.

    Args:
        params (Sequence[nn.Parameter]): The parameters to use for the
            flattened parameter.
        module (nn.Module): A module that is the root of the subtree containing
            all parameters in ``params``; for non-recursive wrapping, this must
            be the top-level module, while for recursive wrapping, this may not
            necessarily be the top-level module.
        device (torch.device): The compute and communication device, which
            should be a non-CPU device. We refer to it as the compute device.
        config (HandleConfig): A config customizing the handle based on FSDP's
            available features.
        use_orig_params (bool): If ``True``, then FSDP preserves the original
            parameter variables and returns them from ``named_parameters()``
            (e.g. to support different optimizer hyperparameters within one
            :class:`FlatParameter`). If ``False``, then FSDP reconstructs the
            parameter every iteration and returns the :class:`FlatParameter` s
            from ``named_parameters()``.
    """

    ##################
    # INITIALIZATION #
    ##################
    def __init__(
        self,
        params: Sequence[nn.Parameter],
        module: nn.Module,
        device: torch.device,
        config: HandleConfig,
        use_orig_params: bool,
    ) -> None:
        super().__init__()
        self.device = device
        self._config = config
        self._use_orig_params = use_orig_params
        self._training_state = HandleTrainingState.IDLE
        self._debug_level = dist.get_debug_level()
        self._init_flat_param(params, module, use_orig_params)
        self._use_unsharded_views(as_params=False)

    def _init_flat_param(
        self,
        params: Sequence[Optional[nn.Parameter]],
        module: nn.Module,
        use_orig_params: bool,
    ) -> None:
        """
        Initializes the flattened parameter ``self.flat_param`` by flattening
        the parameters in ``params`` into a single :class:`FlatParameter` and
        saves relevant metadata. Shared parameters are only included in the
        flattened parameter once.

        This checks that all comprising parameters have the same dtype and
        ``requires_grad`` and does not support nested construction of
        :class:`FlatParameter` s.

        Args:
            See the Args in the class docstring.
        """
        params_set = set(params)
        params_set.discard(None)
        if len(params_set) == 0:
            raise ValueError(
                "Cannot initialize a `FlatParameter` from an empty parameter list"
            )
        param_infos: List[ParamInfo] = []
        numels: List[int] = []
        shapes: List[torch.Size] = []
        prefixed_param_names: List[str] = []
        shared_param_infos: List[SharedParamInfo] = []
        shared_param_memo: Dict[nn.Parameter, Tuple[nn.Module, str, str]] = {}
        params_to_flatten: List[nn.Parameter] = []
        shared_params: List[nn.Parameter] = []
        param_extensions: List[Any] = []
        dtype: Optional[torch.dtype] = None
        requires_grad: Optional[bool] = None
        for submodule_name, submodule in module.named_modules():
            for param_name, param in submodule.named_parameters(recurse=False):
                if param not in params_set:
                    continue
                if param in shared_param_memo:
                    prim_module, prim_module_name, prim_param_name = shared_param_memo[
                        param
                    ]
                    shared_params.append(param)
                    shared_param_infos.append(
                        SharedParamInfo(
                            param_name,
                            submodule,
                            submodule_name,
                            prim_param_name,
                            prim_module,
                            prim_module_name,
                        )
                    )
                else:
                    if type(param) is FlatParameter:
                        raise ValueError("`FlatParameter` does not support nesting")
                    if dtype is not None and param.dtype != dtype:
                        raise ValueError(
                            "`FlatParameter` requires uniform dtype but got "
                            f"{dtype} and {param.dtype}"
                        )
                    if dtype is None:
                        is_floating_point = param.is_floating_point()
                        if not is_floating_point:
                            raise ValueError("Integer parameters are unsupported")
                    if (
                        requires_grad is not None
                        and param.requires_grad != requires_grad
                    ):
                        raise ValueError(
                            "`FlatParameter` requires uniform `requires_grad`"
                        )
                    extension = None
                    if _flattener is not None:
                        extension, new_param = _flattener.pre_flatten_transform(param)
                        if extension is not None:
                            param = new_param
                    param_extensions.append(extension)
                    dtype = param.dtype
                    requires_grad = param.requires_grad
                    shared_param_memo[param] = (submodule, submodule_name, param_name)
                    params_to_flatten.append(param)
                    param_infos.append(ParamInfo(param_name, submodule, submodule_name))
                    numels.append(param.numel())
                    shapes.append(param.shape)
                    prefixed_param_name = (
                        submodule_name + "." + param_name
                        if submodule_name
                        else param_name
                    )
                    prefixed_param_names.append(prefixed_param_name)
        assert requires_grad is not None
        self.flat_param = FlatParamHandle.flatten_params(
            params_to_flatten, requires_grad
        )
        self.flat_param._init_metadata(
            param_infos,
            numels,
            shapes,
            prefixed_param_names,
            shared_param_infos,
            param_extensions,
            params_to_flatten if use_orig_params else None,
            shared_params if use_orig_params else None,
        )

    @staticmethod
    def flatten_params(
        params: Sequence[torch.Tensor],
        requires_grad: bool,
    ) -> FlatParameter:
        """
        Flattens the parameters in ``params`` into a single
        :class:`FlatParameter`. This should be the only way used to construct
        :class:`FlatParameter` s.

        We expose this factory method for checkpointing (e.g. sharded state
        dict). The flattened parameter's metadata should only be initialized
        once (see :meth:`_init_metadata`), but its tensor data may be reloaded.
        """
        with torch.no_grad():
            flat_params = [
                p.detach().reshape(-1) if isinstance(p, nn.Parameter) else p.reshape(-1)
                for p in params
            ]
            flat_param_data = torch.cat(flat_params, dim=0)
        flat_param = FlatParameter(flat_param_data, requires_grad=requires_grad)
        return flat_param

    ###################################
    # SHARD INITIALIZATION & METADATA #
    ###################################
    @torch.no_grad()
    def shard(self, process_group: dist.ProcessGroup):
        """
        Shards the handle's ``FlatParameter``. In terms of memory, this
        allocates new memory for the sharded flattened parameter and frees the
        unsharded flattened parameter's storage.

        Postcondition: ``self.flat_param`` is the sharded flattened parameter.
        Shard metadata attributes are set for all sharding strategies.
        ``process_group``, ``rank``, and ``world_size`` attributes are set if
        using a sharded strategy.

        TODO (awgu): Once we retire ``FlattenParamsWrapper``, we should pass
        the process group directly to the ``FlatParamHandle`` constructor. For
        now, we decouple ``FlattenParamsWrapper` from a process group, but this
        makes the process-group-related attributes not necessarily defined.
        """
        flat_param = self.flat_param
        self.process_group = process_group
        self.rank = process_group.rank()
        self.world_size = process_group.size()
        if not self.uses_sharded_strategy:
            self._init_shard_metadata(0, 0, flat_param.numel() - 1)
        else:
            p_assert(
                flat_param.storage_offset() == 0,
                "The `FlatParameter` is not the sole occupant of its storage",
            )
            orig_storage = flat_param.storage()
            sharded_flat_param, numel_padded = FlatParamHandle._get_shard(
                flat_param, self.rank, self.world_size
            )
            flat_param.set_(sharded_flat_param)  # type: ignore[call-overload]
            start = sharded_flat_param.numel() * self.rank
            end = sharded_flat_param.numel() * (self.rank + 1) - 1  # inclusive
            self._init_shard_metadata(numel_padded, start, end)
            if orig_storage.size() > 0:
                orig_storage.resize_(0)
        if self._use_orig_params:
            self._use_sharded_views()
            self._use_sharded_grad_views()

    def _init_shard_metadata(
        self,
        numel_padded: int,
        start: int,
        end: int,
    ) -> None:
        """
        Initializes shard-related metadata for this rank's shard of the
        flattened parameter: ``_sharded_size``, ``_shard_param_offsets``,
        ``_shard_indices``, and ``_shard_numel_padded``.

        Args:
            numel_padded (int): Numel padded for this rank's sharded flattened
                parameter.
            start (int): Start index in the sharded flattened parameter
                assigned to this rank.
            end (int): End index (inclusive) in the sharded flattened parameter
                assigned to this rank. If this exceeds the sharded flattened
                parameter's numel, then it is truncated.

        Precondition: ``self.flat_param`` 's data is the sharded flattened
        parameter.
        """
        self.flat_param._sharded_size = self.flat_param.size()  # type: ignore[attr-defined]
        sharded_flat_param_numel = self.flat_param.numel()  # includes `numel_padded`
        p_assert(start >= 0 and start <= end, f"start: {start} end: {end}")
        p_assert(
            numel_padded <= sharded_flat_param_numel,
            f"numel_padded: {numel_padded} "
            f"sharded_flat_param_numel: {sharded_flat_param_numel}",
        )
        (
            self.flat_param._shard_param_offsets,  # type: ignore[attr-defined]
            self.flat_param._shard_indices,  # type: ignore[attr-defined]
        ) = self._get_shard_metadata(start, end)
        self.flat_param._shard_numel_padded = numel_padded  # type: ignore[attr-defined]

    def _get_shard_metadata(
        self,
        start: int,
        end: int,
    ) -> Tuple[Tuple[Tuple[int, int], ...], Tuple[int, int]]:
        """
        Computes the shard metadata based on ``start`` and ``end``, which give
        the closed interval of the unsharded flattened parameter specifying the
        shard.

        Args:
            start (int): Start index (in units of numel) of this rank's shard
                of the flattened parameter.
            end (int): End index (in units of numel and inclusive) of this
                rank's shard of the flattened parameter.

        Return:
            Tuple[Tuple[Tuple[int, int], ...], Tuple[int, int]]: See
            ``_shard_param_offsets`` and ``_shard_indices`` in
            :class:`FlatParameter` 's docstring.
        """
        flat_param_offsets = self._get_flat_param_offsets()
        # Indices of the original parameters in this rank's sharded flattened
        # parameter
        shard_param_indices_range = []  # elements will be consecutive
        # [start, end] offsets giving this rank's part of the flattened
        # original module parameter (which will be [0, `p.numel()`-1] for any
        # parameter that is not sharded across ranks)
        shard_param_offsets = []
        for i, (param_start, param_end) in enumerate(flat_param_offsets):
            if start > param_end or end < param_start:
                continue
            if start <= param_start:
                intra_param_start = 0
            else:
                intra_param_start = start - param_start
            intra_param_end = min(param_end, end) - param_start
            shard_param_indices_range.append(i)
            shard_param_offsets.append(
                (intra_param_start, intra_param_end)
            )  # both inclusive
        if len(shard_param_indices_range) == 0:
            shard_param_indices = (0, 0)
            assert len(shard_param_offsets) == 0
        else:
            shard_param_indices = (
                shard_param_indices_range[0],
                shard_param_indices_range[-1],
            )
            assert (
                len(shard_param_offsets)
                == shard_param_indices[-1] - shard_param_indices[0] + 1
            )
        return tuple(shard_param_offsets), shard_param_indices

    @staticmethod
    def _get_unpadded_shard(
        tensor: Tensor,
        rank: int,
        world_size: int,
    ) -> Tuple[Tensor, int]:
        """
        Returns the shard of ``tensor`` without any padding for the given
        ``rank`` and ``world_size`` and the numel to pad for that shard.

        If ``tensor`` is already flattened or may be viewed in the flattened
        shape (which is true in the expected usage), then this method does not
        allocate any new tensor memory.
        """
        chunks = torch.flatten(tensor).chunk(world_size)
        if len(chunks) < (rank + 1):
            # This rank gets an empty chunk fully padded with zeros since there
            # are not enough chunks across ranks
            chunk = chunks[0].new_empty(0)
        else:
            chunk = chunks[rank]
        numel_to_pad = chunks[0].numel() - chunk.numel()
        assert (
            numel_to_pad >= 0
        ), "Chunk's size should be at most the first chunk's size"
        return chunk, numel_to_pad

    @staticmethod
    def _get_shard(
        tensor: Tensor,
        rank: int,
        world_size: int,
    ) -> Tuple[Tensor, int]:
        """
        Returns the shard of ``tensor`` with padding for the given ``rank`` and
        ``world_size`` and the numel padded for that shard.

        This method allocates new memory (via :meth:`clone`) since the
        unsharded ``tensor`` may be deallocated after this method returns.
        """
        chunk, numel_to_pad = FlatParamHandle._get_unpadded_shard(
            tensor, rank, world_size
        )
        shard = chunk.clone()
        if numel_to_pad > 0:
            shard = F.pad(shard, [0, numel_to_pad])
        return shard, numel_to_pad

    @staticmethod
    def _get_sharded_size(tensor: Tensor, rank: int, world_size: int) -> torch.Size:
        """
        Returns the shape of ``tensor`` after sharding including padding. This
        requires ``tensor`` to have 1D shape and ensures that the returned
        shape is 1D.
        """
        assert len(tensor.shape) == 1, f"{tensor.shape}"
        unpadded_sharded_tensor, numel_to_pad = FlatParamHandle._get_unpadded_shard(
            tensor, rank, world_size
        )
        unpadded_sharded_size = unpadded_sharded_tensor.size()
        assert len(unpadded_sharded_size) == 1, f"{unpadded_sharded_size}"
        return torch.Size([unpadded_sharded_size[0] + numel_to_pad])

    def _get_flat_param_offsets(self) -> List[Tuple[int, int]]:
        """Returns [start, end] offsets of each original parameter's flattened
        data in the unsharded flattened parameter (without padding)."""
        cumulative_sum = list(accumulate(self.flat_param._numels))
        starts = [0] + cumulative_sum[:-1]
        ends = [end - 1 for end in cumulative_sum]  # inclusive
        param_offsets = list(zip(starts, ends))
        return param_offsets

    def shard_metadata(
        self,
    ) -> FlatParamShardMetadata:
        """Returns shard-related metadata specific to this rank's shard of the
        flattened parameter."""
        assert hasattr(self.flat_param, "_shard_indices") and hasattr(
            self.flat_param, "_shard_param_offsets"
        ), "Shard metadata has not been initialized"
        shard_param_start_index = self.flat_param._shard_indices[0]  # type: ignore[attr-defined]
        shard_param_end_index = self.flat_param._shard_indices[1]  # type: ignore[attr-defined]
        sl = (
            slice(shard_param_start_index, shard_param_end_index + 1)
            if shard_param_start_index <= shard_param_end_index
            else slice(0, 0)
        )
        return FlatParamShardMetadata(
            self.flat_param._prefixed_param_names[sl],
            self.flat_param._shapes[sl],
            self.flat_param._numels[sl],
            self.flat_param._shard_param_offsets[:],  # type: ignore[attr-defined]
        )

    ###################
    # UNSHARD/RESHARD #
    ###################
    def pre_unshard(self) -> bool:
        """
        Returns: ``False`` if this is a no-op and ``True`` otherwise.

        Postcondition: ``self.flat_param`` 's data is on the device for
        communication and is what should be all-gathered. This means that it
        matches the dtype of the expected unsharded parameter.
        """
        ret = False
        if self._use_orig_params:
            # TODO (awgu): I am not sure if we want to enable this by default
            # for `use_orig_params=True` since there may be some CPU overhead.
            ret = ret or self._writeback_orig_params()
        if (
            self.uses_sharded_strategy
            and not self._config.offload_params
            and not self.needs_unshard()
        ):
            pass  # no-op
        elif self._uses_param_mixed_precision and not self._force_full_precision:
            self._use_low_precision_shard()
            ret = True
        elif self._config.offload_params and self.flat_param.device != self.device:
            # NOTE: This creates a new tensor distinct from any attributes.
            self.flat_param_to(self.device, non_blocking=True)
            ret = True
        self._check_on_compute_device(self.flat_param)
        return ret

    def _use_low_precision_shard(self):
        """
        Allocates the low precision shard directly on the compute device and
        switches to using the low precision sharded flattened parameter.
        """
        self._check_low_precision_shard()
        flat_param = self.flat_param
        _alloc_storage(
            flat_param._mp_shard, flat_param._local_shard.size()  # type: ignore[attr-defined]
        )
        # `copy_()` implicitly casts to the low precision
        flat_param._mp_shard.copy_(  # type: ignore[attr-defined]
            flat_param._local_shard.to(  # type: ignore[attr-defined]
                self.device, non_blocking=True
            )
        )
        # Invariant: `_mp_shard` is always on the compute device.
        flat_param.data = flat_param._mp_shard  # type: ignore[attr-defined]

    def unshard(self, gradient_hook: Optional[Callable]):
        """
        Runs the unshard logic. This includes all-gathering the flattened
        parameter and switching to using the unsharded flattened parameter. If
        the handle does not need unsharding, then this only switches to using
        the unsharded flattened parameter. For ``NO_SHARD``, this is a no-op.

        If FSDP is in :meth:`summon_full_params` and the handle uses parameter
        mixed precision, then the parameter is forced to full precision.
        """
        if not self.needs_unshard():
            # Even when not needing an unshard, we should switch to using
            # the unsharded flattened parameter
            unsharded_flat_param = (
                self._get_padded_unsharded_flat_param()
                if self.uses_sharded_strategy
                else self.flat_param
            )
            self._use_unsharded_flat_param(unsharded_flat_param)
            return
        unsharded_flat_param = self._alloc_padded_unsharded_flat_param()
        padded_unsharded_flat_param = self._all_gather_flat_param(unsharded_flat_param)
        self._use_unsharded_flat_param(padded_unsharded_flat_param)

    def needs_unshard(self) -> bool:
        """Returns if the handle's flattened parameter needs to be unsharded."""
        if not self.uses_sharded_strategy:
            return False
        unsharded_flat_param = self._get_padded_unsharded_flat_param()
        already_unsharded = (
            unsharded_flat_param.storage().size() == unsharded_flat_param.numel()
        )
        return not already_unsharded

    def _alloc_padded_unsharded_flat_param(self):
        """
        Allocates the *padded* unsharded flattened parameter. The unpadded
        unsharded flattened parameter is always a view into the padded one.
        This padded parameter is saved to a different attribute on the
        ``FlatParameter`` depending on if we force full precision.
        """
        self._check_sharded_strategy()
        flat_param = self.flat_param
        unsharded_flat_param = self._get_padded_unsharded_flat_param()
        self._check_storage_freed(unsharded_flat_param)
        _alloc_storage(unsharded_flat_param, flat_param._padded_unsharded_size)  # type: ignore[attr-defined]
        return unsharded_flat_param

    def _get_padded_unsharded_flat_param(self) -> torch.Tensor:
        """
        Returns a reference to the padded unsharded flattened parameter
        depending on the calling context. This should only be called if using a
        sharded strategy.
        """
        self._check_sharded_strategy()
        flat_param = self.flat_param
        if self._force_full_precision:
            # When parameter mixed precision is enabled, we use a different
            # tensor as the all-gather destination to preserve the invariant
            # that  `_full_param_padded` is in the low precision
            unsharded_flat_param = flat_param._full_prec_full_param_padded  # type: ignore[attr-defined]
            p_assert(
                unsharded_flat_param.dtype != self._config.param_dtype,
                f"Expects full precision but got {self._config.param_dtype}",
            )
        else:
            unsharded_flat_param = flat_param._full_param_padded  # type: ignore[attr-defined]
        return unsharded_flat_param

    def _all_gather_flat_param(
        self,
        padded_unsharded_flat_param: Tensor,
    ) -> Tensor:
        """
        All-gathers the handle's flattened parameter to the destination
        ``padded_unsharded_flat_param``, and switches to using the all-gathered
        tensor.
        """
        p_assert(
            hasattr(self, "process_group") and hasattr(self, "world_size"),
            "Expects a process group and world size to have been set via `shard()`",
        )
        sharded_flat_param = self.flat_param.data
        expected_numel = sharded_flat_param.numel() * self.world_size
        p_assert(
            padded_unsharded_flat_param.numel() == expected_numel,
            f"Expects {expected_numel} numel but got {padded_unsharded_flat_param.numel()}",
        )
        dist._all_gather_base(
            padded_unsharded_flat_param,
            sharded_flat_param,
            self.process_group,
        )
        return padded_unsharded_flat_param

    def _use_unsharded_flat_param(
        self,
        padded_unsharded_flat_param: torch.Tensor,
    ) -> None:
        """
        Switches to using the *unpadded* unsharded flattened parameter, which
        is a view into the *padded* unsharded flattened parameter.
        """
        unsharded_size = self.flat_param._unpadded_unsharded_size
        self.flat_param.data = padded_unsharded_flat_param[
            : unsharded_size.numel()
        ].view(unsharded_size)  # this `.view()` is not autograd visible
        # TODO (awgu): For `use_orig_params=True`, we create the unsharded
        # views here, not through the FPW. This enables a *single* place for
        # unsharded view creation during runtime. We should coalesce the
        # `use_orig_params=False` code path to do the same.
        if self._use_orig_params:
            if self._training_state != HandleTrainingState.FORWARD:
                # NOTE: `as_params=True` suffices here because we only need to
                # restore the tensor *values* for backward computation. It
                # does not need to be a fresh `Tensor` view.
                self._use_unsharded_views(as_params=True)
            else:
                self._use_unsharded_views(as_params=False)

    def post_unshard(self):
        """
        Runs the post-unshard logic. This includes freeing the low precision
        shard if needed.
        """
        if self._uses_param_mixed_precision and self.uses_sharded_strategy:
            self._free_low_precision_sharded_param()
        self._check_on_compute_device(self.flat_param)

    def _free_low_precision_sharded_param(self):
        """Frees the low precision sharded flattened parameter."""
        self._check_low_precision_shard()
        _free_storage(self.flat_param._mp_shard)  # type: ignore[attr-defined]

    def prepare_gradient_for_backward(self):
        """
        Prepares the gradient for the backward computation by saving and
        clearing any existing sharded gradient in ``.grad`` to enable computing
        a new unsharded gradient.
        """
        p_assert(
            self._training_state
            in (HandleTrainingState.BACKWARD_PRE, HandleTrainingState.IDLE),
            "Expects to be in `BACKWARD_PRE` or `IDLE` (if prefetching)",
        )
        flat_param = self.flat_param
        if flat_param.grad is not None and (
            flat_param.grad.size() != flat_param._unpadded_unsharded_size
            or flat_param.grad.device != flat_param.device  # grad on CPU
        ):
            self._check_on_compute_device(self.flat_param)
            grad_offloaded = flat_param.grad.device != self.device
            p_assert(
                not grad_offloaded or self._config.offload_params,
                f"Expects the sharded gradient to be on {self.device} "
                f"but got {flat_param.grad.device}",
            )
            prev_iter_synced_gradients = (
                flat_param.grad.size()
                == flat_param._local_shard.size()  # type: ignore[attr-defined]
            )
            if prev_iter_synced_gradients:
                # TODO (awgu): Gradient accumulation outside `no_sync()`
                # does not work with CPU offloading. The issue should be
                # that, in the post-backward hook, we cannot do an addition
                # between a CPU tensor (the existing sharded gradient) and
                # a GPU tensor (the new sharded gradient).
                if not grad_offloaded:
                    flat_param._saved_grad_shard = flat_param.grad.data  # type: ignore[attr-defined]
                    # If we're using mixed precision with keeping grads
                    # casted, gradient here might still be of the reduced
                    # dtype if we didn't clear / set the gradients to None
                    # after previous forward. In that case, make sure
                    # p._saved_grad_shard is cast to the full precision type
                    # so that we can accumulate in full precision in
                    # _post_backward_hook and assign back in full precision
                    # in _wait_for_post_backward.
                    if (
                        self._config.keep_low_precision_grads and
                        flat_param._saved_grad_shard.dtype != flat_param._local_shard.dtype  # type: ignore[attr-defined]
                    ):
                        flat_param._saved_grad_shard = (  # type: ignore[attr-defined]
                            flat_param._saved_grad_shard.to(flat_param._local_shard.dtype)  # type: ignore[attr-defined]
                        )
            else:
                padded_unsharded_size = flat_param._padded_unsharded_size  # type: ignore[attr-defined]
                p_assert(
                    flat_param.grad.size() == padded_unsharded_size,
                    "Expects `.grad` to be the unsharded gradient in "
                    f"`no_sync()` with size {padded_unsharded_size} "
                    f"but got size {flat_param.grad.size()}",
                )
            flat_param.grad = None

    def prepare_gradient_for_optim(self):
        """
        Prepares the gradient for optimizer computation by moving the sharded
        gradient to the ``.grad`` attribute.
        """
        flat_param = self.flat_param
        # TODO (awgu): We should replace these conditional checks to encode
        # the logical intention more directly.
        if hasattr(flat_param, "_cpu_grad"):
            # NOTE: This branch includes `NO_SHARD`.
            self._check_sharded(flat_param)
            self._check_on_cpu(flat_param)
            flat_param.grad = flat_param._cpu_grad  # type: ignore[attr-defined]
        elif hasattr(flat_param, "_saved_grad_shard"):
            self._check_sharded(flat_param)
            self._check_on_compute_device(flat_param)
            self._check_on_compute_device(flat_param._saved_grad_shard)  # type: ignore[attr-defined]
            # If no sharded gradient was computed this iteration, then there is
            # no need to forward `_saved_grad_shard` to `grad`
            if flat_param._post_backward_called:  # type: ignore[attr-defined]
                flat_param.grad = flat_param._saved_grad_shard  # type: ignore[attr-defined]
                if self._config.keep_low_precision_grads:
                    assert flat_param.grad is not None
                    flat_param.grad.data = flat_param.grad.to(self._config.param_dtype)

        else:
            p_assert(
                not self.uses_sharded_strategy
                or not flat_param._post_backward_called,  # type: ignore[attr-defined]
                "All sharded parameters that received a gradient in the "
                "post-backward should use `_saved_grad_shard`",
            )
        if self._use_orig_params:
            self._use_sharded_grad_views()
        # TODO (awgu): I am not sure that deleting this is necessary.
        if hasattr(flat_param, "_saved_grad_shard"):
            delattr(flat_param, "_saved_grad_shard")

    @contextlib.contextmanager
    def to_cpu(self):
        """
        Moves the unpadded unsharded flattened parameter to CPU while in the
        context and moves it back to the previous device upon exit. For now,
        this assumes the ``FlatParameter`` is the unpadded unsharded flattened
        parameter since (1) there is no reason to include the padding in the
        copy and (2) there is no use case for the sharded flattened parameter.

        Precondition: ``self.flat_param`` 's data is the unpadded unsharded
        flattened parameter on the compute device, and the handle uses a
        sharded strategy.
        Postcondition: Same as the precondition.
        """
        self._check_sharded_strategy()
        p_assert(
            self.flat_param.size() == self.flat_param._unpadded_unsharded_size,
            f"Expects size {self.flat_param._unpadded_unsharded_size} but got {self.flat_param.size()}",
        )
        self._check_on_compute_device(self.flat_param)
        # Check that the unpadded unsharded flattened parameter is a view into
        # the padded unsharded flattened parameter as expected
        # NOTE: This check is not strictly needed for correctness but is a
        # useful sanity check since the tensor should only be used internally.
        unpadded_storage_ptr = self.flat_param.storage().data_ptr()
        padded_storage_ptr = (
            self._get_padded_unsharded_flat_param().storage().data_ptr()
        )
        p_assert(
            unpadded_storage_ptr == padded_storage_ptr,
            "Expects the unpadded parameter to be a view into the padded parameter",
        )
        self.flat_param_to(torch.device("cpu"))
        self._free_unsharded_flat_param()
        try:
            yield
        finally:
            p_assert(
                self.flat_param.size() == self.flat_param._unpadded_unsharded_size,
                f"Expects size {self.flat_param._unpadded_unsharded_size} but got {self.flat_param.size()}",
            )
            padded_unsharded_flat_param = self._alloc_padded_unsharded_flat_param()
            # Copy from CPU to the compute device
            padded_unsharded_flat_param[: self.flat_param.numel()].copy_(
                self.flat_param
            )
            self._use_unsharded_flat_param(padded_unsharded_flat_param)

    def reshard(self, free_unsharded_flat_param: bool):
        """
        Runs the reshard logic. This includes freeing the unsharded flattened
        parameter if ``free_unsharded_flat_param`` and switching to using the
        sharded flattened parameter.
        """
        if free_unsharded_flat_param:
            self._free_unsharded_flat_param()
        self._use_sharded_flat_param()

    def post_reshard(self):
        """
        Runs the post-reshard logic. This includes freeing any memory that
        can now be freed given that the ``FlatParameter`` points to the full
        precision sharded flattened parameter.

        Precondition: ``self.flat_param`` 's data points to the full precision
        sharded flattened parameter.
        """
        # For `NO_SHARD`, `_mp_shard` is not freed in the post-unshard since
        # it is also the low precision *unsharded* flattened parameter. Hence,
        # we delay the free until the reshard.
        if (
            self._uses_param_mixed_precision
            and not self.uses_sharded_strategy
            and not self._force_full_precision  # did not use the low precision shard
        ):
            self._free_low_precision_sharded_param()

    def _free_unsharded_flat_param(self):
        """
        Frees the padded unsharded flattened parameter. The tensor to free
        depends on the calling context since the unshard may have forced full
        precision, in which case a different tensor is used.
        """
        self._check_sharded_strategy()
        unsharded_flat_param = self._get_padded_unsharded_flat_param()
        self._check_storage_allocated(unsharded_flat_param)
        self._check_on_compute_device(unsharded_flat_param)
        # Do not free the memory until all ops in the current stream finish
        unsharded_flat_param.record_stream(
            cast(torch._C.Stream, torch.cuda.current_stream())
        )
        _free_storage(unsharded_flat_param)

    def _use_sharded_flat_param(self) -> None:
        """Switches to using the sharded flattened parameter."""
        flat_param = self.flat_param
        if self._config.offload_params:
            device = flat_param._local_shard.device  # type: ignore[attr-defined]
            p_assert(
                device == torch.device("cpu"),
                f"Expects the local shard to be on CPU but got {device}",
            )
        flat_param.data = flat_param._local_shard  # type: ignore[attr-defined]
        if self._use_orig_params:
            self._use_sharded_views()

    #########
    # VIEWS #
    #########
    @staticmethod
    def _get_unflat_views(
        flat_param: FlatParameter,
        tensor: Optional[torch.Tensor] = None,
    ) -> Iterator[Tensor]:
        """
        Returns unflattened ``Tensor`` views into ``tensor`` if it is not
        ``None`` or ``flat_param`` otherwise, where the unflattening is based
        on ``flat_param`` 's metadata.

        In other words, to get views into the unsharded flattened parameter,
        pass ``tensor`` as ``None``, but to get views into tensor optimizer
        state, pass ``tensor`` as the optimizer state tensor.
        """
        if tensor is None:
            tensor = flat_param
        p_assert(
            tensor.numel() == flat_param._unpadded_unsharded_size.numel(),
            f"Expects {flat_param._unpadded_unsharded_size.numel()} numel but got "
            f"{tensor.numel()} numel",
        )
        views = (
            subtensor.view(shape)
            for (subtensor, shape) in zip(
                torch.split(tensor, flat_param._numels, dim=0), flat_param._shapes  # type: ignore[arg-type]
            )
        )
        return views

    def _use_unsharded_views(self, as_params: bool) -> None:
        """
        Unflattens the unsharded flattened parameter by setting the original
        module parameter variables to be views into it.

        Args:
            as_params (bool): If ``True``, then registers the original
                parameters as ``nn.Parameter`` s; if ``False``, then registers
                the original parameters only as ``Tensor`` s. ``False`` should
                be used during forward/backward computation and when hiding the
                original parameters from :meth:`nn.Module.named_parameters`.
        """
        # if dist.get_rank() == 0:
        #     print(f"[Rank {dist.get_rank()}] _use_unsharded_views(as_params={as_params}) {self._training_state}!")
        self._check_unsharded()
        views = self._get_unflat_views(self.flat_param)
        for i, (view, (param_name, module, _)) in enumerate(
            zip(views, self.flat_param._param_infos)
        ):
            if _flattener is not None:
                param_extension = self.flat_param._param_extensions[i]
                if param_extension is not None:
                    view = _flattener.post_unflatten_transform(view, param_extension)
            if hasattr(module, param_name):
                delattr(module, param_name)
            if self._use_orig_params and as_params:
                param = self.flat_param._params[i]  # type: ignore[index]
                setattr(module, param_name, param)
                param.data = view
            elif as_params:
                module.register_parameter(param_name, nn.Parameter(view))
            else:
                setattr(module, param_name, view)
        for i, (
            param_name,
            module,
            _,
            prim_param_name,
            prim_module,
            prim_module_name,
        ) in enumerate(self.flat_param._shared_param_infos):
            if hasattr(module, param_name):
                delattr(module, param_name)
            p_assert(
                hasattr(prim_module, prim_param_name),
                f"Module {prim_module_name} is missing parameter {prim_param_name}",
            )
            prim_param: Union[Tensor, nn.Parameter] = getattr(
                prim_module, prim_param_name
            )
            p_assert(
                not as_params or isinstance(prim_param, nn.Parameter),
                f"as_params={as_params} type(prim_param)={type(prim_param)}",
            )
            if self._use_orig_params and as_params:
                shared_param = self.flat_param._shared_params[i]  # type: ignore[index]
                setattr(module, param_name, shared_param)
                shared_param.data = prim_param
            elif as_params:
                assert isinstance(prim_param, nn.Parameter)
                module.register_parameter(param_name, prim_param)
            else:
                setattr(module, param_name, prim_param)

    @contextlib.contextmanager
    def unflatten_as_params(self) -> Generator:
        """
        Assumes the flattened parameter is unsharded. When in the context,
        unflattens the original parameters as ``nn.Parameter`` views into the
        flattened parameter, and after the context, restores the original
        parameters as ``Tensor`` views into the flattened parameter.
        """
        self._use_unsharded_views(as_params=True)
        try:
            yield
        finally:
            self._use_unsharded_views(as_params=False)

    @torch.no_grad()
    def _use_sharded_views(self):
        """
        Sets the original module parameter variables' data to be flattened
        views into the sharded flattened parameter.

        The views are kept as flattened to simplify the case where a parameter
        is sharded across ranks. Parameters whose data is not present in the
        sharded flattened parameter have their data set to a size-0 empty
        tensor. We do not delete them to ensure to preserve expected behaviors
        like model printability. Parameters whose data is present must preserve
        their variables to be passable to an optimizer.
        """
        # if dist.get_rank() == 0:
        #     print(f"[Rank {dist.get_rank()}] _use_sharded_views()! {self._training_state}")
        if not self.uses_sharded_strategy:
            # For `NO_SHARD`, use the *unflattened* unsharded views since we
            # have the unsharded parameter
            self._use_unsharded_views(as_params=True)
            return
        self._check_sharded(self.flat_param)
        start, end = self.flat_param._shard_indices  # type: ignore[attr-defined]
        offset = 0
        assert self.flat_param._params is not None
        for i, (param, (param_name, module, _)) in enumerate(
            zip(self.flat_param._params, self.flat_param._param_infos)
        ):
            setattr(module, param_name, param)
            in_sharded_flat_param = i >= start and i <= end
            if in_sharded_flat_param:
                param_start, param_end = self.flat_param._shard_param_offsets[i - start]  # type: ignore[attr-defined]
                numel_in_shard = param_end - param_start + 1
                param.data = self.flat_param[offset : offset + numel_in_shard]
                offset += numel_in_shard
            else:
                # Allow the original data to be freed via garbage collection
                param.data = torch.empty(
                    0,
                    dtype=param.dtype,
                    device=self.flat_param.device,
                    requires_grad=False,
                )
        assert self.flat_param._shared_params is not None
        for i, (
            param,
            (param_name, module, _, prim_param_name, prim_module, _),
        ) in enumerate(
            zip(self.flat_param._shared_params, self.flat_param._shared_param_infos)
        ):
            setattr(module, param_name, param)
            prim_param = getattr(prim_module, prim_param_name)
            param.data = prim_param  # could be both empty and non-empty

    @torch.no_grad()
    def _use_sharded_grad_views(self):
        """
        Sets the original module parameter variables' gradients to be flattened
        views into the sharded flattened parameter's gradient. This is a no-op
        if there is no gradient.

        Parameters whose data is not present in the sharded flattened parameter
        and parameters with ``requires_grad=False`` have their gradients set to
        ``None``. Since the gradient variables do not need to be preserved,
        this method does not manipulate existing ``Tensor`` data directly and
        creates new ``Tensor`` variables instead.
        """
        # if dist.get_rank() == 0:
        #     print(f"[Rank 0] _use_sharded_grad_views()!")
        self._check_sharded(self.flat_param)
        if self.flat_param.grad is None:
            return  # no-op
        self._check_sharded(self.flat_param.grad)
        start, end = self.flat_param._shard_indices  # type: ignore[attr-defined]
        offset = 0
        assert self.flat_param._params is not None
        for i, param in enumerate(self.flat_param._params):
            in_sharded_flat_param = i >= start and i <= end
            if in_sharded_flat_param:
                param_start, param_end = self.flat_param._shard_param_offsets[i - start]  # type: ignore[attr-defined]
                numel_in_shard = param_end - param_start + 1
                if param.requires_grad:
                    param.grad = (
                        self.flat_param.grad[offset : offset + numel_in_shard].reshape(param.shape)
                    )
                else:
                    param.grad = None
                offset += numel_in_shard
            else:
                param.grad = None
        assert self.flat_param._shared_params is not None
        for i, (param, (_, _, _, prim_param_name, prim_module, _)) in enumerate(
            zip(self.flat_param._shared_params, self.flat_param._shared_param_infos)
        ):
            in_sharded_flat_param = hasattr(prim_module, prim_param_name)
            if in_sharded_flat_param and param.requires_grad:
                prim_param = getattr(prim_module, prim_param_name)
                param.grad = prim_param.grad  # share the same reference
            else:
                param.grad = None

    @torch.no_grad()
    def _writeback_orig_params(self):
        """
        Iterates over the original parameters and writes back any parameters
        that changed storages (due to a non-inplace operator) to the handle's
        ``FlatParameter``. This method preserves the ``FlatParameter` 's
        device even if an original parameter's device changes.

        Raises:
            RuntimeError: An original parameter changes storages and no longer
            has the expected flattened shape.
        Returns: ``True`` if some writeback happened, and ``False`` otherwise.
        """
        if not self.is_sharded(self.flat_param):
            return False
        flat_param = self.flat_param
        start, end = flat_param._shard_indices  # type: ignore[attr-defined]
        offset = 0
        assert flat_param._params is not None
        wroteback = False
        for i, (param, (param_name, module, _)) in enumerate(
            zip(flat_param._params, flat_param._param_infos)
        ):
            if not hasattr(module, param_name):
                # Do not writeback if original parameters are deregistered
                # (e.g. during model checkpointing)
                continue
            in_sharded_flat_param = i >= start and i <= end
            if in_sharded_flat_param:
                param_start, param_end = flat_param._shard_param_offsets[i - start]  # type: ignore[attr-defined]
                numel_in_shard = param_end - param_start + 1
                # Check for parameter writeback
                param_changed = getattr(module, param_name) is not param
                needs_param_writeback = (
                    param_changed  # changed parameter variable itself
                    or not _same_storage(param, flat_param)  # changed `.data`
                )
                if param_changed:
                    # NOTE: After a parameter change, the gradient is not
                    # preserved.
                    param = getattr(module, param_name)
                    flat_param._params[i] = param
                if needs_param_writeback:
                    # TODO (awgu): Should we relax this to just having the same
                    # numel? Then, we just flatten and writeback.
                    expected_shape = torch.Size([numel_in_shard])
                    if self._debug_level == [dist.DebugLevel.INFO, dist.DebugLevel.DETAIL]:
                        rank = self.rank if hasattr(self, "rank") else dist.get_rank()
                        print(
                            f"[Rank {rank}] needs writeback in {self._training_state}; "
                            f"expected shape={expected_shape} shape={param.shape} "
                            f"expected device={flat_param.device} device={param.device}"
                        )
                    if param.shape != expected_shape:
                        raise RuntimeError(
                            "Cannot writeback when the parameter shape changes\n"
                            f"Expects {expected_shape} but got {param.shape}"
                        )
                    flat_param[offset : offset + numel_in_shard].copy_(param)
                    wroteback = True
                # Check for gradient writeback
                # NOTE: Since this method is called in the pre-unshard, which
                # is only called in the pre-forward or pre-backward, the
                # sharded gradient should be guaranteed to be in `.grad`, not
                # in `._saved_grad_shard`.
                if param.grad is None and flat_param.grad is not None:
                    warnings.warn(
                        "Setting an original parameter's `.grad` to `None` "
                        "does not set the underlying `FlatParameter`'s `.grad` "
                        "to `None`."
                    )
                elif param.grad is not None:
                    needs_grad_writeback = (
                        flat_param.grad is None
                        or not _same_storage(param.grad, flat_param.grad)
                    )
                    # TODO (awgu): Allocate a new zeroed sharded gradient for
                    # `flat_param.grad` if it is `None`, and write the data
                    # into the appropriate view.
                    if needs_grad_writeback:
                        raise NotImplementedError("Gradient writeback is not implemented")
                offset += numel_in_shard
        # TODO (awgu): Shared parameters could become no longer shared??
        assert flat_param._shared_param_infos is not None
        for i, (param, (param_name, module, _, _, _, _)) in enumerate(
            zip(flat_param._shared_params, flat_param._shared_param_infos)  # type: ignore[arg-type]
        ):
            if not hasattr(module, param_name):
                # Do not writeback if original parameters are deregistered
                # (e.g. during model checkpointing)
                continue
            param_changed = getattr(module, param_name) is not param
            if param_changed:
                flat_param._shared_params[i] = getattr(module, param_name)  # type: ignore[index]
        return wroteback

    def _deregister_orig_params(self):
        for (param_name, module, _) in self.flat_param._param_infos:
            if hasattr(module, param_name):
                delattr(module, param_name)
        for (param_name, module, _, _, _, _) in self.flat_param._shared_param_infos:
            if hasattr(module, param_name):
                delattr(module, param_name)

    ###########
    # HELPERS #
    ###########
    def flat_param_to(self, *args, **kwargs):
        """Wraps an in-place call to ``.to()`` for ``self.flat_param``."""
        self.flat_param.data = self.flat_param.to(*args, **kwargs)
        if self._use_orig_params:
            # Refresh the views because their storage may have changed
            if self.is_sharded(self.flat_param):
                self._use_sharded_views()
            else:
                self._use_unsharded_views(as_params=True)

    def _get_modules(self) -> Set[nn.Module]:
        """Returns a :class:`set` of the modules whose parameters are included
        in this handle's flattened parameter."""
        return set(pi.module for pi in self.flat_param._param_infos).union(
            set(spi.module for spi in self.flat_param._shared_param_infos)
        )

    def is_sharded(self, tensor: Tensor) -> bool:
        """
        Returns if ``tensor`` is *currently* sharded. For ``NO_SHARD``, this
        always returns ``True`` once :meth:`shard` has been called.
        """
        if not hasattr(self.flat_param, "_sharded_size"):
            # `_sharded_size` is defined iff `handle.shard()` has been called
            return False
        sharded_size = self.flat_param._sharded_size  # type: ignore[attr-defined]
        return tensor.size() == sharded_size

    def parameter_module_names(self) -> Iterator[Tuple[str, str]]:
        shared_param_infos = [
            ParamInfo(param_name, module, module_name)
            for (
                param_name,
                module,
                module_name,
                _,
                _,
                _,
            ) in self.flat_param._shared_param_infos
        ]
        for param_name, _, module_name in chain(
            self.flat_param._param_infos, shared_param_infos
        ):
            yield (param_name, module_name)

    #######################
    # CHECKS & INVARIANTS #
    #######################
    def _check_sharded_strategy(self):
        p_assert(self.uses_sharded_strategy, "Expects sharded strategy")

    def _check_on_compute_device(self, tensor: Tensor):
        p_assert(
            tensor.device == self.device,
            f"Expects tensor to be on the compute device {self.device}",
        )

    def _check_on_cpu(self, tensor: Tensor):
        p_assert(
            tensor.device == torch.device("cpu"),
            f"Expects tensor to be on CPU but got {tensor.device}",
        )

    @staticmethod
    def _check_storage_freed(tensor: Tensor):
        storage_size: int = tensor.storage().size()
        p_assert(
            storage_size == 0,
            f"Expects storage to be freed but got storage with size {storage_size}",
        )

    @staticmethod
    def _check_storage_allocated(tensor: Tensor):
        storage_size: int = tensor.storage().size()
        p_assert(storage_size > 0, "Expects storage to be allocated")

    def _check_low_precision_shard(self):
        p_assert(
            self._uses_param_mixed_precision,
            "Not using low precision for parameters",
        )
        p_assert(
            getattr(self.flat_param, "_mp_shard", None) is not None,
            "Expects `_mp_shard` to exist",
        )
        device = self.flat_param._mp_shard.device  # type: ignore[attr-defined]
        p_assert(
            device == self.device,
            f"Expects the low precision shard to be on {self.device} but got {device}",
        )

    def _check_unsharded(self):
        msg_prefix = "Expects the flattened parameter to be unsharded "
        p_assert(self.flat_param is not None, msg_prefix + "but got `None`")
        unsharded_size = self.flat_param._unpadded_unsharded_size
        p_assert(
            self.flat_param.size() == unsharded_size,
            msg_prefix + f"with size {unsharded_size} but got {self.flat_param.size()}",
        )

    def _check_sharded(self, tensor: Tensor):
        msg_prefix = "Expects tensor to be sharded "
        p_assert(tensor is not None, msg_prefix + "but got `None`")
        sharded_size = self.flat_param._sharded_size  # type: ignore[attr-defined]
        p_assert(
            tensor.size() == sharded_size,
            msg_prefix + f"with size {sharded_size} but got {tensor.size()}",
        )

    ##############
    # PROPERTIES #
    ##############
    @property
    def uses_sharded_strategy(self) -> bool:
        return self._config.sharding_strategy != HandleShardingStrategy.NO_SHARD

    @property
    def _uses_param_mixed_precision(self) -> bool:
        return self._config.param_dtype is not None

    @property
    def _force_full_precision(self) -> bool:
        return (
            self._training_state == HandleTrainingState.SUMMON_FULL_PARAMS
            and self._uses_param_mixed_precision
        )
