from gacha_assets.env import resolve_supabase_key


def test_storage_api_key_is_the_default():
    assert resolve_supabase_key(environ={"SUPABASE_STORAGE_API_KEY": "storage-key"}) == "storage-key"


def test_service_role_key_is_supported_as_fallback():
    assert resolve_supabase_key(environ={"SUPABASE_SERVICE_ROLE_KEY": "service-key"}) == "service-key"


def test_supabase_secret_key_is_supported_as_fallback():
    assert resolve_supabase_key(environ={"SUPABASE_SECRET_KEY": "sb_secret_test"}) == "sb_secret_test"


def test_explicit_key_name_and_fallback_are_supported():
    values = {"CUSTOM_STORAGE_KEY": "custom-key", "SUPABASE_SERVICE_ROLE_KEY": "service-key"}
    assert resolve_supabase_key("CUSTOM_STORAGE_KEY", values) == "custom-key"
    assert resolve_supabase_key("SUPABASE_STORAGE_API_KEY", {"SUPABASE_SERVICE_ROLE_KEY": "service-key"}) == "service-key"
