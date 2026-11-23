"""GMO エラーエンベロープ構造化パースのユニットテスト（Issue #136, 付録B E04/E06）."""

from __future__ import annotations

from typing import Any

import pytest

from backend.src.gmo_errors import (
    GmoApiError,
    extract_message_codes,
    raise_for_gmo_status,
)

# 付録B E04 §1 実採取レスポンス（認証失敗時）と同形
_ERROR_BODY: dict[str, Any] = {
    "status": 1,
    "messages": [
        {
            "message_code": "ERR-5012",
            "message_string": "Invalid API-KEY, IP, or permissions for action.",
        }
    ],
    "responsetime": "2026-06-29T07:00:00.000Z",
}


class TestExtractMessageCodes:
    def test_extracts_codes_from_appendix_b_shape(self) -> None:
        assert extract_message_codes(_ERROR_BODY) == ("ERR-5012",)

    def test_multiple_codes_preserved_in_order(self) -> None:
        body = {
            "status": 1,
            "messages": [
                {"message_code": "ERR-5106", "message_string": "a"},
                {"message_code": "ERR-5126", "message_string": "b"},
            ],
        }
        assert extract_message_codes(body) == ("ERR-5106", "ERR-5126")

    @pytest.mark.parametrize(
        "body",
        [
            None,
            "not a dict",
            {},
            {"messages": "not a list"},
            {"messages": [{"message_string": "code なし"}, "not a dict", {"message_code": 123}]},
        ],
    )
    def test_malformed_bodies_yield_empty(self, body: Any) -> None:
        assert extract_message_codes(body) == ()


class TestRaiseForGmoStatus:
    def test_status_zero_does_not_raise(self) -> None:
        raise_for_gmo_status({"status": 0, "data": {}}, context="test")

    def test_status_nonzero_raises_with_structured_codes(self) -> None:
        with pytest.raises(GmoApiError) as exc_info:
            raise_for_gmo_status(_ERROR_BODY, context="account/assets")
        assert exc_info.value.status == 1
        assert exc_info.value.message_codes == ("ERR-5012",)
        assert "account/assets" in str(exc_info.value)

    def test_rate_limit_status_4_raises(self) -> None:
        """付録B E06 §1: レートリミット超過は status:4（ERR-5003）."""
        body = {
            "status": 4,
            "messages": [{"message_code": "ERR-5003", "message_string": "Requests are too many."}],
        }
        with pytest.raises(GmoApiError) as exc_info:
            raise_for_gmo_status(body, context="ifoOrder")
        assert exc_info.value.status == 4
        assert exc_info.value.message_codes == ("ERR-5003",)

    def test_missing_status_key_is_skipped_for_mock_compat(self) -> None:
        """status キーなし（テスト用モック）は検査しない."""
        raise_for_gmo_status({"data": []}, context="test")

    def test_non_dict_body_is_skipped(self) -> None:
        raise_for_gmo_status("plain text", context="test")
        raise_for_gmo_status(None, context="test")

    def test_httpx_like_response_object_is_parsed(self) -> None:
        """.json() を持つオブジェクト（httpx.Response 互換）からもボディを検査する."""

        class FakeResponse:
            def json(self) -> dict[str, Any]:
                return dict(_ERROR_BODY)

        with pytest.raises(GmoApiError) as exc_info:
            raise_for_gmo_status(FakeResponse(), context="closeOrder")
        assert exc_info.value.message_codes == ("ERR-5012",)

    def test_unparseable_json_is_skipped(self) -> None:
        """ボディが JSON でない場合は検査しない（HTTP 層の検査は呼び出し元の責務）."""

        class BadResponse:
            def json(self) -> dict[str, Any]:
                raise ValueError("not json")

        raise_for_gmo_status(BadResponse(), context="test")
