from atv_player.plugins.spider_crypto.keyring import DEFAULT_KID, load_default_keyring


def test_load_default_keyring_uses_embedded_material() -> None:
    keyring = load_default_keyring()

    assert keyring.get_master_secret(DEFAULT_KID) == b"63490bac-88a8-45fe-8794-4ebe9c4ffbf3"
    assert "BEGIN PUBLIC KEY" in keyring.get_public_key(DEFAULT_KID).export_key(format="PEM")
