import base64

from app import avatar
from .conftest import CSRF, add_user

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 40
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 40


def data_url(mime, raw):
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def login(client, email="a@test.uz", password="parol-12345"):
    return client.post("/portal/auth/login", json={"email": email, "password": password}, headers=CSRF)


def test_sniff_accepts_only_png_jpeg_webp():
    assert avatar.sniff(PNG) == "image/png"
    assert avatar.sniff(JPEG) == "image/jpeg"
    assert avatar.sniff(WEBP) == "image/webp"
    assert avatar.sniff(b"<svg xmlns='http://www.w3.org/2000/svg'/>") is None


def test_upload_read_and_remove_own_avatar(env):
    add_user(env)
    login(env.client)
    assert env.client.get("/portal/auth/me").json()["user"]["avatar"] is None
    assert env.client.get("/portal/account/avatar").status_code == 404

    r = env.client.put("/portal/account/avatar", json={"image": data_url("image/png", PNG)}, headers=CSRF)
    assert r.status_code == 200 and r.json()["avatar"]
    ver = env.client.get("/portal/auth/me").json()["user"]["avatar"]
    assert ver == r.json()["avatar"]

    got = env.client.get("/portal/account/avatar")
    assert got.status_code == 200 and got.content == PNG
    assert got.headers["content-type"] == "image/png"
    assert got.headers["x-content-type-options"] == "nosniff"
    assert env.client.get("/portal/account/avatar", headers={"If-None-Match": got.headers["etag"]}).status_code == 304

    # a second upload moves the version forward even within the same second
    r2 = env.client.put("/portal/account/avatar", json={"image": data_url("image/jpeg", JPEG)}, headers=CSRF)
    assert r2.json()["avatar"] > ver
    assert env.client.get("/portal/account/avatar").headers["content-type"] == "image/jpeg"

    assert env.client.delete("/portal/account/avatar", headers=CSRF).json() == {"avatar": None}
    assert env.client.get("/portal/account/avatar").status_code == 404


def test_avatar_rejects_wrong_type_size_and_missing_csrf(env):
    add_user(env)
    login(env.client)
    put = lambda image, headers=CSRF: env.client.put("/portal/account/avatar", json={"image": image}, headers=headers)  # noqa: E731
    assert put(data_url("image/png", JPEG)).status_code == 400            # declared type does not match the bytes
    assert put(data_url("image/svg+xml", b"<svg/>")).status_code == 400   # SVG is never accepted
    assert put("data:image/png;base64,@@@").status_code == 400
    big = PNG + b"\x00" * avatar.MAX_BYTES
    assert put(data_url("image/png", big)).status_code in (400, 422)
    assert put(data_url("image/png", PNG), headers={}).status_code == 403


def test_avatar_needs_a_session(env):
    assert env.client.get("/portal/account/avatar").status_code == 401
    assert env.client.put("/portal/account/avatar", json={"image": data_url("image/png", PNG)}, headers=CSRF).status_code == 401


def test_avatars_are_private_to_their_owner(env):
    add_user(env, "a@test.uz")
    add_user(env, "b@test.uz")
    login(env.client, "a@test.uz")
    env.client.put("/portal/account/avatar", json={"image": data_url("image/png", PNG)}, headers=CSRF)
    env.client.post("/portal/auth/logout", headers=CSRF)
    login(env.client, "b@test.uz")
    assert env.client.get("/portal/account/avatar").status_code == 404
