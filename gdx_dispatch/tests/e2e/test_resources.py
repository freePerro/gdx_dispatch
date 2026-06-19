"""E2E tests for Resource Library endpoints.

Covers:
- GET /api/resources
- GET /api/resources/{id}/download
- POST /api/resources (admin only)
- DELETE /api/resources/{id} (admin only)
- Non-admin authorization enforcement
- download_count increment on download

Also validates the Chrome extension file set in archive/dispatch_flask/ext/ used for packaging.
"""
from __future__ import annotations

import io
import uuid
import zipfile
from pathlib import Path

import httpx
import pytest

from gdx_dispatch.tests.e2e.conftest import (
    BASE_URL,
    TENANT_ID,
    APIClient,
    assert_api_success,
)

pytestmark = [pytest.mark.e2e]


EXT_REQUIRED_FILES = [
    "manifest.json",
    "background.js",
    "popup.html",
    "popup.js",
    "popup.css",
    "content_scripts/chi_portal.js",
    "lib/gdx_api.js",
    "lib/chi_config.js",
    "lib/clopay_config.js",
    "lib/amarr_config.js",
    "lib/wd_config.js",
    "icons/icon-16.png",
    "icons/icon-48.png",
    "icons/icon-128.png",
]


@pytest.fixture(scope="module")
def ext_root() -> Path:
    """Resolve repository extension directory for packaging tests."""
    repo_root = Path(__file__).resolve().parents[3]
    root = repo_root / "dispatch" / "ext"
    if not root.exists():
        pytest.skip(f"archive/dispatch_flask/ext not found at {root}")
    return root


@pytest.fixture(scope="module")
def extension_zip_bytes(ext_root: Path) -> bytes:
    """Build a ZIP payload from required extension files for resource upload tests."""
    missing = [rel for rel in EXT_REQUIRED_FILES if not (ext_root / rel).exists()]
    assert not missing, f"Missing expected extension files in archive/dispatch_flask/ext: {missing}"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel in EXT_REQUIRED_FILES:
            zf.write(ext_root / rel, arcname=rel)

    payload = buf.getvalue()
    assert len(payload) > 0
    return payload


def _upload_resource(
    api: APIClient,
    file_bytes: bytes,
    *,
    name: str,
    category: str = "extension",
    description: str = "E2E extension package",
    version: str = "1.0.0-e2e",
    filename: str = "gdx-extension-e2e.zip",
    content_type: str = "application/zip",
) -> httpx.Response:
    """Upload resource via multipart form using APIClient."""
    return api.post(
        "/api/resources",
        params={
            "name": name,
            "description": description,
            "category": category,
            "version": version,
        },
        files={
            "file": (filename, io.BytesIO(file_bytes), content_type),
        },
    )


@pytest.fixture(scope="module")
def non_admin_api(api: APIClient):
    """Create a non-admin user and return an authenticated APIClient for that user."""
    unique = uuid.uuid4().hex[:8]
    email = f"e2e_tech_{unique}@example.com"
    password = f"TechOnly_{unique}!"

    create_resp = api.post(
        "/api/admin/users",
        json_data={
            "username": f"e2e-tech-{unique}",
            "email": email,
            "password": password,
            "role": "technician",
        },
    )
    if create_resp.status_code != 201:
        pytest.skip(f"Cannot create non-admin test user: {create_resp.status_code}")
        return
    user_id = create_resp.json()["id"]

    try:
        with httpx.Client(base_url=BASE_URL, verify=False, timeout=15) as client:
            login_resp = client.post(
                "/auth/login",
                json={"email": email, "password": password},
                headers={"x-tenant-id": TENANT_ID, "Content-Type": "application/json"},
            )
        assert login_resp.status_code == 200, (
            f"Failed to login non-admin test user: {login_resp.status_code} {login_resp.text[:300]}"
        )
        token = login_resp.json().get("access_token", "")
        assert token, "Non-admin login response missing access_token"

        client = APIClient(BASE_URL, token, TENANT_ID)
        yield client
        client.close()
    finally:
        api.delete(f"/api/admin/users/{user_id}")


class TestResourcePackaging:
    def test_res_00_extension_files_present_for_packaging(self, ext_root: Path, extension_zip_bytes: bytes):
        """Verify archive/dispatch_flask/ext has required files and they can be packaged into ZIP bytes."""
        for rel in EXT_REQUIRED_FILES:
            assert (ext_root / rel).exists(), f"Missing extension file: {rel}"
        assert len(extension_zip_bytes) > 0, "Packaged extension ZIP is empty"


class TestResourcesAPI:
    def test_res_01_listing_resources(self, api: APIClient, authenticated_page, extension_zip_bytes: bytes):
        """GET /api/resources returns list payload and includes uploaded item."""
        token_present = authenticated_page.evaluate("""() => !!sessionStorage.getItem('gdx_access_token')""")
        assert token_present, "authenticated_page fixture did not inject auth token"

        unique = uuid.uuid4().hex[:8]
        create_resp = _upload_resource(
            api,
            extension_zip_bytes,
            name=f"GDX Extension Package {unique}",
        )
        assert create_resp.status_code == 201, (
            f"Resource create failed: {create_resp.status_code} {create_resp.text[:300]}"
        )
        created = create_resp.json()

        list_resp = api.get("/api/resources")
        assert_api_success(list_resp)
        data = list_resp.json()

        assert isinstance(data, dict)
        assert "items" in data
        assert "total" in data
        assert isinstance(data["items"], list)
        assert isinstance(data["total"], int)

        ids = [item.get("id") for item in data["items"]]
        assert created["id"] in ids, "Created resource missing from /api/resources listing"

    def test_res_02_download_resource(self, api: APIClient, extension_zip_bytes: bytes):
        """GET /api/resources/{id}/download returns file payload."""
        unique = uuid.uuid4().hex[:8]
        create_resp = _upload_resource(
            api,
            extension_zip_bytes,
            name=f"Download Test Resource {unique}",
        )
        assert create_resp.status_code == 201
        resource = create_resp.json()

        download_resp = api.get(f"/api/resources/{resource['id']}/download")
        assert_api_success(download_resp)
        assert len(download_resp.content) > 0, "Downloaded resource is empty"
        assert download_resp.content == extension_zip_bytes, "Downloaded bytes differ from uploaded bytes"

    def test_res_03_create_resource_admin_only(self, api: APIClient, extension_zip_bytes: bytes):
        """POST /api/resources succeeds for admin and returns ResourceOut."""
        unique = uuid.uuid4().hex[:8]
        resp = _upload_resource(
            api,
            extension_zip_bytes,
            name=f"Admin Create Resource {unique}",
            description="Admin-created extension ZIP",
            category="extension",
            version="2.0.0-e2e",
        )
        assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text[:300]}"

        data = resp.json()
        assert data["id"]
        assert data["name"].startswith("Admin Create Resource")
        assert data["category"] == "extension"
        assert data["mime_type"] in ("application/zip", "application/x-zip-compressed")
        assert data["download_count"] == 0

    def test_res_04_delete_resource_admin_only(self, api: APIClient, extension_zip_bytes: bytes):
        """DELETE /api/resources/{id} soft-deletes resource for admin."""
        unique = uuid.uuid4().hex[:8]
        create_resp = _upload_resource(
            api,
            extension_zip_bytes,
            name=f"Delete Test Resource {unique}",
        )
        assert create_resp.status_code == 201
        rid = create_resp.json()["id"]

        delete_resp = api.delete(f"/api/resources/{rid}")
        assert delete_resp.status_code == 204, (
            f"Expected 204, got {delete_resp.status_code}: {delete_resp.text[:300]}"
        )

        get_resp = api.get(f"/api/resources/{rid}")
        assert get_resp.status_code == 404, (
            f"Deleted resource should be hidden; got {get_resp.status_code}"
        )

    def test_res_05_non_admin_cannot_create_or_delete_resources(
        self,
        api: APIClient,
        non_admin_api: APIClient,
        extension_zip_bytes: bytes,
    ):
        """Non-admin users cannot POST/DELETE resources (403)."""
        unique = uuid.uuid4().hex[:8]

        non_admin_create = _upload_resource(
            non_admin_api,
            extension_zip_bytes,
            name=f"Should Fail Create {unique}",
        )
        assert non_admin_create.status_code == 403, (
            f"Non-admin create should be 403, got {non_admin_create.status_code}: "
            f"{non_admin_create.text[:300]}"
        )

        admin_create = _upload_resource(
            api,
            extension_zip_bytes,
            name=f"Admin Created For NonAdmin Delete {unique}",
        )
        assert admin_create.status_code == 201
        rid = admin_create.json()["id"]

        non_admin_delete = non_admin_api.delete(f"/api/resources/{rid}")
        assert non_admin_delete.status_code == 403, (
            f"Non-admin delete should be 403, got {non_admin_delete.status_code}: "
            f"{non_admin_delete.text[:300]}"
        )

        cleanup_delete = api.delete(f"/api/resources/{rid}")
        assert cleanup_delete.status_code in (204, 404)

    def test_res_06_download_increments_download_count(self, api: APIClient, extension_zip_bytes: bytes):
        """Download endpoint increments resources.download_count."""
        unique = uuid.uuid4().hex[:8]
        create_resp = _upload_resource(
            api,
            extension_zip_bytes,
            name=f"Count Test Resource {unique}",
        )
        assert create_resp.status_code == 201
        rid = create_resp.json()["id"]

        before_resp = api.get(f"/api/resources/{rid}")
        assert_api_success(before_resp)
        before_count = int(before_resp.json().get("download_count", 0))

        first_dl = api.get(f"/api/resources/{rid}/download")
        assert_api_success(first_dl)
        second_dl = api.get(f"/api/resources/{rid}/download")
        assert_api_success(second_dl)

        after_resp = api.get(f"/api/resources/{rid}")
        assert_api_success(after_resp)
        after_count = int(after_resp.json().get("download_count", 0))

        assert after_count == before_count + 2, (
            f"download_count did not increment by 2: before={before_count}, after={after_count}"
        )
