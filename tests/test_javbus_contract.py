"""JavBus 插件协议契约测试（API_INTEGRATION_STANDARD §10）。

全部离线：不联网、不启动后端，只校验清单、Provider 契约与纯函数行为。
运行方式（在项目根目录）：python -m pytest comic_backend/third_party/javbus/tests -q
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys

import pytest

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_ROOT = os.path.abspath(os.path.join(PLUGIN_DIR, "..", ".."))
PROJECT_ROOT = os.path.dirname(BACKEND_ROOT)
for _root in (BACKEND_ROOT, PROJECT_ROOT):
    if _root not in sys.path:
        sys.path.insert(0, _root)

protocol_base = pytest.importorskip("protocol.base")

MANIFEST_PATH = os.path.join(PLUGIN_DIR, "ultimate-plugin.json")
PROVIDER_PATH = os.path.join(PLUGIN_DIR, "ultimate_provider.py")

PLUGIN_ID = "video.javbus"
ENTRYPOINT = "./ultimate_provider.py:JavBusProvider"
PROTOCOL_VERSIONS = {"1.0", "1.1", "2.0"}
FIELD_TYPES = {"boolean", "text", "password", "textarea", "number"}
SECRET_FIELDS = ("cookie_string",)
REQUIRED_FIELDS: tuple = ()
RUNTIME_DEPENDENCIES = ("curl-cffi", "beautifulsoup4", "lxml")


@pytest.fixture(scope="module")
def manifest() -> dict:
    with open(MANIFEST_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture(scope="module")
def plugin_module():
    spec = importlib.util.spec_from_file_location("_javbus_plugin_under_test", PROVIDER_PATH)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


@pytest.fixture()
def provider(manifest, plugin_module):
    return plugin_module.JavBusProvider(manifest=manifest, manifest_path=MANIFEST_PATH)


def _capability_keys(manifest: dict) -> set:
    return {str(item.get("key") or "").strip() for item in manifest["capabilities"]}


def _configuration_fields(manifest: dict) -> list:
    return [
        field
        for section in manifest["configuration"]["sections"]
        for field in section.get("fields") or []
    ]


# ---------- 清单契约 ----------

def test_manifest_minimum_contract(manifest):
    assert manifest["protocol_version"] in PROTOCOL_VERSIONS
    plugin = manifest["plugin"]
    assert plugin["id"] == PLUGIN_ID
    assert plugin["entrypoint"] == ENTRYPOINT
    assert plugin["config_key"] == "javbus"
    assert plugin["version"]
    assert manifest["media_types"] == ["video"]
    assert manifest["identity"]["host_id_prefix"] == "BUS"
    assert manifest["identity"]["platform_label"] == "JavBus"


def test_capabilities_match_provider_constant(manifest, plugin_module):
    assert _capability_keys(manifest) == set(plugin_module.SUPPORTED_CAPABILITIES)


def test_capability_dispatch_covers_declared_set(plugin_module):
    with open(PROVIDER_PATH, "r", encoding="utf-8") as handle:
        source = handle.read()
    dispatched = set(re.findall(r'capability\s*==\s*"([^"]+)"', source))
    assert dispatched == set(plugin_module.SUPPORTED_CAPABILITIES)


def test_proxy_stream_capability_is_not_declared(manifest, provider):
    # JavBus 没有在线播放能力，旧版 501 桩会让宿主 /proxy/<domain>/<path> 报错
    assert "playback.proxy.stream" not in _capability_keys(manifest)
    assert not hasattr(provider, "_handle_proxy_stream")
    with pytest.raises(ValueError):
        provider.execute("playback.proxy.stream", {}, {}, {"enabled": True})


def test_configuration_field_types_are_supported(manifest):
    for field in _configuration_fields(manifest):
        assert field["type"] in FIELD_TYPES, field


def test_movie_type_field_is_text(manifest):
    fields = {field["key"]: field for field in _configuration_fields(manifest)}
    assert fields["movie_type"]["type"] == "text"
    assert "select" not in {field["type"] for field in fields.values()}


def test_configuration_credential_block_is_consistent(manifest):
    credential = manifest["configuration"]["credential"]
    fields = _configuration_fields(manifest)
    boolean_fields = {field["key"] for field in fields if field["type"] == "boolean"}
    assert credential["enabled_field"] in boolean_fields
    assert set(credential.get("required_fields") or []) <= {field["key"] for field in fields}
    assert credential["required_fields"] == list(REQUIRED_FIELDS)
    assert credential["disabled_message"]


def test_secret_fields_are_flagged(manifest):
    secrets = {field["key"] for field in _configuration_fields(manifest) if field.get("secret")}
    assert secrets == set(SECRET_FIELDS)


def test_packaging_covers_runtime_imports(manifest):
    packaging = manifest["packaging"]
    assert packaging["android"]["enabled"] is True
    declared = set()
    for platform in ("android", "external", "pyinstaller"):
        requirements = packaging[platform]["pip_requirements"]
        assert requirements and all(str(item).strip() for item in requirements)
        declared |= {str(item).strip().lower().replace("_", "-") for item in requirements}
    for name in RUNTIME_DEPENDENCIES:
        assert name in declared


def test_presentation_declares_mobile_aspect_ratio(manifest):
    cover = manifest["presentation"]["media_card"]["cover"]
    assert cover["aspect_ratio"]
    assert cover["mobile_aspect_ratio"]


def test_resource_policy_uses_host_asset_keys(manifest):
    assets = manifest["resource_policy"]["assets"]
    assert set(assets) <= {"image", "cover", "preview_video", "video", "asset"}


# ---------- Provider 契约 ----------

def test_provider_inherits_protocol_provider(provider):
    assert isinstance(provider, protocol_base.ProtocolProvider)
    for method in ("execute", "normalize_config", "serialize_public_config", "get_query_status", "build_client"):
        assert callable(getattr(provider, method))


def test_undeclared_capability_is_rejected(provider):
    with pytest.raises(ValueError):
        provider.execute("catalog.by_code", {}, {}, {"enabled": True})


def test_enabled_defaults_to_disabled(provider):
    assert provider.normalize_config({})["enabled"] is False
    assert provider.get_query_status({})["configured"] is False


def test_health_status_answers_while_disabled(provider):
    status = provider.execute("health.query.status", {}, {}, {})
    assert set(status) >= {"configured", "message", "missing_fields"}
    assert status["configured"] is False


def test_disabled_provider_refuses_catalog(provider):
    with pytest.raises(RuntimeError):
        provider.execute("catalog.search", {"keyword": "x"}, {}, {"enabled": False})


def test_cookie_is_not_wiped_by_empty_or_masked_save(provider):
    assert "cookie_string" not in provider.normalize_config({"enabled": True})
    assert "cookie_string" not in provider.normalize_config({"enabled": True, "cookie_string": ""})
    assert "cookie_string" not in provider.normalize_config({"enabled": True, "cookie_string": "******"})
    assert (
        provider.normalize_config({"enabled": True, "cookie_string": "existmag=all"})["cookie_string"]
        == "existmag=all"
    )


def test_public_config_hides_cookie(provider):
    public = provider.serialize_public_config({"enabled": True, "cookie_string": "existmag=all"})
    assert "cookie_string" not in public
    assert public["cookie_configured"] is True
    assert provider.serialize_public_config({"enabled": True})["cookie_configured"] is False


def test_movie_type_selects_uncensored_section(provider):
    assert provider._domain({"domain": "https://www.javbus.com"}) == "https://www.javbus.com"
    assert (
        provider._domain({"domain": "https://www.javbus.com", "movie_type": "uncensored"})
        == "https://www.javbus.com/uncensored"
    )
    assert provider.normalize_config({"movie_type": "huh"})["movie_type"] == "normal"
    assert provider.normalize_config({"movie_type": "uncensored"})["movie_type"] == "uncensored"


def test_detail_requires_video_id(provider):
    with pytest.raises(RuntimeError):
        provider.execute(
            "catalog.detail", {}, {}, {"enabled": True, "domain": "https://www.javbus.com"}
        )


def test_http_request_requires_url(provider):
    with pytest.raises(ValueError):
        provider.execute(
            "transport.http.request", {}, {}, {"enabled": True, "domain": "https://www.javbus.com"}
        )


def test_cover_fetch_requires_arguments(provider):
    with pytest.raises(RuntimeError):
        provider.execute(
            "asset.cover.fetch",
            {"video_id": "ABC-123"},
            {},
            {"enabled": True, "domain": "https://www.javbus.com"},
        )


def test_make_soup_parses_with_available_parser(plugin_module):
    soup = plugin_module._make_soup("<html><body><p>ok</p></body></html>")
    assert soup.find("p").get_text() == "ok"
