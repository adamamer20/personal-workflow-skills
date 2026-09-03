from __future__ import annotations

import pytest

from codex_flow.ipc import MAX_FRAME_BYTES, IpcError, IpcReasonCode, decode_frame, encode_frame, recv_exact


class _FragmentedReader:
    def __init__(self, payload: bytes, *, chunk_size: int = 3) -> None:
        self._chunks = [payload[index : index + chunk_size] for index in range(0, len(payload), chunk_size)]

    def recv(self, size: int) -> bytes:
        if not self._chunks:
            return b""
        chunk = self._chunks.pop(0)
        if len(chunk) > size:
            self._chunks.insert(0, chunk[size:])
            return chunk[:size]
        return chunk


def test_ipc_frame_round_trip_is_strict_and_bounded() -> None:
    frame = encode_frame({"operation": "wake", "version": 1})
    assert decode_frame(_FragmentedReader(frame)) == {"operation": "wake", "version": 1}


def test_ipc_rejects_oversized_and_fragmented_frames() -> None:
    with pytest.raises(IpcError, match="bounded payload") as encoded:
        encode_frame({"payload": "x" * MAX_FRAME_BYTES})
    assert encoded.value.reason_code == IpcReasonCode.FRAME_OVERSIZED.value
    oversized_header = (MAX_FRAME_BYTES + 1).to_bytes(4, "big")
    with pytest.raises(IpcError, match="bounded payload") as decoded:
        decode_frame(_FragmentedReader(oversized_header))
    assert decoded.value.reason_code == IpcReasonCode.FRAME_OVERSIZED.value


def test_ipc_classifies_non_json_response_separately_from_oversized_frame() -> None:
    malformed = (2).to_bytes(4, "big") + b"{]"
    with pytest.raises(IpcError, match="strict UTF-8 JSON") as error:
        decode_frame(_FragmentedReader(malformed))
    assert error.value.reason_code == IpcReasonCode.RESPONSE_NOT_JSON.value


def test_recv_exact_rejects_peer_close_mid_frame() -> None:
    with pytest.raises(IpcError, match="fragmented frame"):
        recv_exact(_FragmentedReader(b"short"), 10)
