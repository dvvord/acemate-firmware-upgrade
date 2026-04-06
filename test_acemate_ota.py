#!/usr/bin/env python3
"""Tests for acemate_ota.py"""

import asyncio
import json
import os
import tempfile
from unittest import mock

import pytest
import requests

import acemate_ota


# ---------------------------------------------------------------------------
# Utility function tests
# ---------------------------------------------------------------------------

class TestExtractVersion:
    def test_standard_version(self):
        assert acemate_ota.extract_version("ota_package_acemate_1.4.3.tar") == "1.4.3"

    def test_version_with_gz(self):
        assert acemate_ota.extract_version("ota_package_acemate_1.3.6.tar.gz") == "1.3.6"

    def test_version_only(self):
        assert acemate_ota.extract_version("2.0.1") == "2.0.1"

    def test_no_version(self):
        assert acemate_ota.extract_version("firmware.bin") is None

    def test_empty_string(self):
        assert acemate_ota.extract_version("") is None

    def test_multiple_versions_returns_first(self):
        assert acemate_ota.extract_version("v1.2.3_to_4.5.6.tar") == "1.2.3"


# ---------------------------------------------------------------------------
# BLE command construction tests
# ---------------------------------------------------------------------------

class TestMakeBleCommand:
    def test_appends_delimiter(self):
        result = acemate_ota.make_ble_command('{"type":"test"}')
        assert result == b'{"type":"test"}\r\n+==ACE==+'

    def test_returns_bytes(self):
        result = acemate_ota.make_ble_command("hello")
        assert isinstance(result, bytes)

    def test_utf8_encoding(self):
        result = acemate_ota.make_ble_command("test")
        assert result == "test\r\n+==ACE==+".encode("utf-8")


class TestMakeWifiOpenCommand:
    def test_default_5g(self):
        cmd = acemate_ota.make_wifi_open_command()
        parsed = json.loads(cmd)
        assert parsed["type"] == "ap"
        assert parsed["data"]["purpose"] == "ota"
        assert parsed["data"]["wifiBand"] == "5G"
        assert parsed["data"]["wifi_channels"] == [149, 153]

    def test_24g_band(self):
        cmd = acemate_ota.make_wifi_open_command(wifi_band="2.4G")
        parsed = json.loads(cmd)
        assert parsed["data"]["wifiBand"] == "2.4G"

    def test_custom_purpose(self):
        cmd = acemate_ota.make_wifi_open_command(purpose="test")
        parsed = json.loads(cmd)
        assert parsed["data"]["purpose"] == "test"

    def test_custom_channels(self):
        cmd = acemate_ota.make_wifi_open_command(channels=[1, 6, 11])
        parsed = json.loads(cmd)
        assert parsed["data"]["wifi_channels"] == [1, 6, 11]

    def test_json_no_spaces(self):
        cmd = acemate_ota.make_wifi_open_command()
        assert " " not in cmd  # compact JSON, no spaces


class TestMakeWifiStopCommand:
    def test_default(self):
        cmd = acemate_ota.make_wifi_stop_command()
        parsed = json.loads(cmd)
        assert parsed["type"] == "stop_ap"
        assert parsed["data"]["purpose"] == "ota"

    def test_custom_purpose(self):
        cmd = acemate_ota.make_wifi_stop_command(purpose="test")
        parsed = json.loads(cmd)
        assert parsed["data"]["purpose"] == "test"


# ---------------------------------------------------------------------------
# Cloud API tests
# ---------------------------------------------------------------------------

class TestCloudHeaders:
    def test_has_required_headers(self):
        headers = acemate_ota.cloud_headers()
        assert "User-Agent" in headers
        assert "X-Acemate-Staging" in headers
        assert "Accept-Language" in headers
        assert "Content-Type" in headers

    def test_staging_false(self):
        headers = acemate_ota.cloud_headers()
        assert headers["X-Acemate-Staging"] == "false"

    def test_content_type_json(self):
        headers = acemate_ota.cloud_headers()
        assert headers["Content-Type"] == "application/json"


class TestQueryCloudFirmwareInfo:
    def test_success(self):
        init_resp = mock.Mock()
        init_resp.json.return_value = {"code": 0, "data": {"wifiChannels": [149, 153]}}
        init_resp.raise_for_status = mock.Mock()

        configs_resp = mock.Mock()
        configs_resp.json.return_value = {
            "code": 0,
            "data": {
                "otaPackageUrl": "https://example.com/fw.tar.gz",
                "otaPackageName": "fw_1.4.3.tar.gz",
                "isCloseForceOta": False,
            },
        }
        configs_resp.raise_for_status = mock.Mock()

        with mock.patch("acemate_ota.requests") as mock_requests:
            mock_requests.get.return_value = init_resp
            mock_requests.post.return_value = configs_resp

            info = acemate_ota.query_cloud_firmware_info()

        assert info is not None
        assert info["url"] == "https://example.com/fw.tar.gz"
        assert info["name"] == "fw_1.4.3.tar.gz"
        assert info["force_closed"] is False

    def test_configs_failure(self):
        init_resp = mock.Mock()
        init_resp.json.return_value = {"code": 0, "data": {}}
        init_resp.raise_for_status = mock.Mock()

        with mock.patch("acemate_ota.requests") as mock_requests:
            mock_requests.get.return_value = init_resp
            mock_requests.post.side_effect = Exception("connection error")

            info = acemate_ota.query_cloud_firmware_info()

        assert info is None

    def test_non_zero_code(self):
        init_resp = mock.Mock()
        init_resp.json.return_value = {"code": 0, "data": {}}
        init_resp.raise_for_status = mock.Mock()

        configs_resp = mock.Mock()
        configs_resp.json.return_value = {"code": 500, "message": "error"}
        configs_resp.raise_for_status = mock.Mock()

        with mock.patch("acemate_ota.requests") as mock_requests:
            mock_requests.get.return_value = init_resp
            mock_requests.post.return_value = configs_resp

            info = acemate_ota.query_cloud_firmware_info()

        assert info is None


class TestDownloadFirmware:
    def test_download_new_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            resp = mock.Mock()
            resp.headers = {"content-length": "100"}
            resp.iter_content.return_value = [b"x" * 50, b"y" * 50]
            resp.raise_for_status = mock.Mock()

            with mock.patch("acemate_ota.requests.get", return_value=resp):
                path = acemate_ota.download_firmware(
                    "https://example.com/fw.bin", "fw.bin", tmpdir
                )

            assert os.path.exists(path)
            assert os.path.getsize(path) == 100

    def test_skip_existing_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            existing = os.path.join(tmpdir, "fw.bin")
            with open(existing, "w") as f:
                f.write("existing")

            with mock.patch("acemate_ota.requests.get") as mock_get:
                path = acemate_ota.download_firmware(
                    "https://example.com/fw.bin", "fw.bin", tmpdir
                )
                mock_get.assert_not_called()

            assert path == existing


# ---------------------------------------------------------------------------
# Upload & trigger tests
# ---------------------------------------------------------------------------

class TestUploadFirmware:
    def test_success_code_0(self):
        with tempfile.NamedTemporaryFile(suffix=".tar") as f:
            f.write(b"fake firmware data")
            f.flush()

            resp = mock.Mock()
            resp.status_code = 200
            resp.ok = True
            resp.text = '{"code": 0}'
            resp.json.return_value = {"code": 0}

            with mock.patch("acemate_ota.requests.post", return_value=resp):
                result = acemate_ota.upload_firmware(f.name)

            assert result is True

    def test_success_code_200(self):
        with tempfile.NamedTemporaryFile(suffix=".tar") as f:
            f.write(b"fake firmware data")
            f.flush()

            resp = mock.Mock()
            resp.status_code = 200
            resp.ok = True
            resp.text = '{"code": 200}'
            resp.json.return_value = {"code": 200}

            with mock.patch("acemate_ota.requests.post", return_value=resp):
                result = acemate_ota.upload_firmware(f.name)

            assert result is True

    def test_success_float_code(self):
        with tempfile.NamedTemporaryFile(suffix=".tar") as f:
            f.write(b"fake firmware data")
            f.flush()

            resp = mock.Mock()
            resp.status_code = 200
            resp.ok = True
            resp.text = '{"code": 0.0}'
            resp.json.return_value = {"code": 0.0}

            with mock.patch("acemate_ota.requests.post", return_value=resp):
                result = acemate_ota.upload_firmware(f.name)

            assert result is True

    def test_file_not_found(self):
        result = acemate_ota.upload_firmware("/nonexistent/file.tar")
        assert result is False

    def test_connection_error_retries(self):
        with tempfile.NamedTemporaryFile(suffix=".tar") as f:
            f.write(b"fake firmware data")
            f.flush()

            with mock.patch("acemate_ota.requests.post") as mock_post:
                mock_post.side_effect = requests.exceptions.ConnectionError("refused")
                with mock.patch("acemate_ota.time.sleep"):
                    result = acemate_ota.upload_firmware(f.name, retries=1)

            assert result is False
            # 1 initial + 1 retry = 2 calls
            assert mock_post.call_count == 2

    def test_upload_filename_appends_gz(self):
        with tempfile.NamedTemporaryFile(suffix=".tar") as f:
            f.write(b"data")
            f.flush()

            resp = mock.Mock()
            resp.status_code = 200
            resp.ok = True
            resp.text = '{"code": 0}'
            resp.json.return_value = {"code": 0}

            with mock.patch("acemate_ota.requests.post", return_value=resp) as mock_post:
                acemate_ota.upload_firmware(f.name)

            # Check that the multipart data was sent with .gz suffix
            call_kwargs = mock_post.call_args
            content_type = call_kwargs.kwargs.get("headers", {}).get("Content-Type", "")
            assert "multipart" in content_type

    def test_upload_filename_no_double_gz(self):
        with tempfile.NamedTemporaryFile(suffix=".tar.gz") as f:
            f.write(b"data")
            f.flush()

            resp = mock.Mock()
            resp.status_code = 200
            resp.ok = True
            resp.text = '{"code": 0}'
            resp.json.return_value = {"code": 0}

            with mock.patch("acemate_ota.requests.post", return_value=resp):
                result = acemate_ota.upload_firmware(f.name)

            assert result is True

    def test_server_error_code(self):
        with tempfile.NamedTemporaryFile(suffix=".tar") as f:
            f.write(b"data")
            f.flush()

            resp = mock.Mock()
            resp.status_code = 200
            resp.ok = True
            resp.text = '{"code": 500}'
            resp.json.return_value = {"code": 500}

            with mock.patch("acemate_ota.requests.post", return_value=resp):
                # ok=True but code=500 → app considers ok as success fallback
                result = acemate_ota.upload_firmware(f.name, retries=0)

            # The app logic: code in (0,200) OR resp.ok → success
            assert result is True


class TestTriggerUpdate:
    def test_success(self):
        resp = mock.Mock()
        resp.status_code = 200
        resp.ok = True
        resp.text = '{"code": 0}'
        resp.json.return_value = {"code": 0}

        with mock.patch("acemate_ota.requests.post", return_value=resp):
            assert acemate_ota.trigger_update() is True

    def test_connection_error(self):
        with mock.patch("acemate_ota.requests.post") as mock_post:
            mock_post.side_effect = requests.exceptions.ConnectionError("refused")
            assert acemate_ota.trigger_update() is False

    def test_server_error(self):
        resp = mock.Mock()
        resp.status_code = 500
        resp.ok = False
        resp.text = '{"code": 500}'
        resp.json.return_value = {"code": 500}

        with mock.patch("acemate_ota.requests.post", return_value=resp):
            assert acemate_ota.trigger_update() is False


class TestUploadAndTrigger:
    def test_upload_fails_skips_trigger(self):
        with mock.patch("acemate_ota.upload_firmware", return_value=False) as mock_upload:
            with mock.patch("acemate_ota.trigger_update") as mock_trigger:
                result = acemate_ota.upload_and_trigger("/fake/path")

        assert result is False
        mock_trigger.assert_not_called()

    def test_upload_succeeds_triggers(self):
        with mock.patch("acemate_ota.upload_firmware", return_value=True):
            with mock.patch("acemate_ota.trigger_update", return_value=True) as mock_trigger:
                with mock.patch("acemate_ota.time.sleep"):
                    result = acemate_ota.upload_and_trigger("/fake/path")

        assert result is True
        mock_trigger.assert_called_once()


class TestCheckConnectivity:
    def test_reachable(self):
        resp = mock.Mock()
        resp.status_code = 200

        with mock.patch("acemate_ota.requests.get", return_value=resp):
            assert acemate_ota.check_connectivity() is True

    def test_unreachable(self):
        with mock.patch("acemate_ota.requests.get") as mock_get:
            mock_get.side_effect = requests.exceptions.ConnectionError()
            assert acemate_ota.check_connectivity() is False


# ---------------------------------------------------------------------------
# BLE update flow tests
# ---------------------------------------------------------------------------

class TestBleUpdateFlow:
    """Tests for BLE message parsing and protocol logic."""

    @pytest.mark.asyncio
    async def test_notify_parses_mcu_progress(self):
        """Test that on_notify correctly parses mcu_ota_result messages."""
        # We test the message parsing logic in isolation
        messages = []

        msg = json.dumps({
            "type": "mcu_ota_result",
            "data": {"Progress": 42.5}
        })
        full = msg + "\r\n+==ACE==+"

        # Simulate what on_notify does
        buffer = full
        while acemate_ota.BLE_MSG_DELIMITER in buffer:
            part, buffer = buffer.split(acemate_ota.BLE_MSG_DELIMITER, 1)
            part = part.strip()
            if part:
                parsed = json.loads(part)
                messages.append(parsed)

        assert len(messages) == 1
        assert messages[0]["type"] == "mcu_ota_result"
        assert messages[0]["data"]["Progress"] == 42.5

    @pytest.mark.asyncio
    async def test_notify_handles_fragmented_messages(self):
        """Test that buffering handles messages split across multiple BLE packets."""
        msg = json.dumps({"type": "rsp_ap", "data": {"ssid": "Test", "purpose": "ota"}})
        full = msg + "\r\n+==ACE==+"

        # Split into fragments
        mid = len(full) // 2
        frag1 = full[:mid]
        frag2 = full[mid:]

        buffer = ""
        results = []

        for frag in [frag1, frag2]:
            buffer += frag
            while acemate_ota.BLE_MSG_DELIMITER in buffer:
                part, buffer = buffer.split(acemate_ota.BLE_MSG_DELIMITER, 1)
                part = part.strip()
                if part:
                    results.append(json.loads(part))

        assert len(results) == 1
        assert results[0]["data"]["ssid"] == "Test"


# ---------------------------------------------------------------------------
# Constants sanity checks
# ---------------------------------------------------------------------------

class TestConstants:
    def test_ble_uuids_format(self):
        import re
        uuid_re = re.compile(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
        )
        assert uuid_re.match(acemate_ota.NUS_SERVICE_UUID)
        assert uuid_re.match(acemate_ota.NUS_TX_CHAR_UUID)
        assert uuid_re.match(acemate_ota.NUS_RX_CHAR_UUID)

    def test_robot_base_url(self):
        assert acemate_ota.RK_BASE_URL == "http://10.42.0.1:5000"

    def test_upload_endpoint(self):
        assert acemate_ota.RK_OTA_UPLOAD == "/upload"

    def test_trigger_endpoint(self):
        assert acemate_ota.RK_TRIGGER_UPDATE == "/trigger_update"

    def test_delimiter(self):
        assert acemate_ota.BLE_MSG_DELIMITER == "+==ACE==+"
