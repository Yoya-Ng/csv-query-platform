"""前端用到的後端新增功能：期別清單（取自分割界限）、上傳續傳狀態。"""
from __future__ import annotations

import hashlib

import pytest

from app.services import upload
from tests.test_import import P, load, seed, write

pytestmark = pytest.mark.db


def test_list_periods_from_partition_bounds(conn, adapter, settings, dataset, tmp_path):
    assert adapter.list_periods(conn, dataset) == []
    seed(conn, adapter, settings, dataset, tmp_path)
    _, st = load(conn, adapter, settings, dataset, write(tmp_path, "p9.csv", "yyyymm,policy_no\n202609,A\n"), period=202609)
    assert st == "CLEANED"
    conn.execute(f"ANALYZE {dataset.name}")
    got = adapter.list_periods(conn, dataset)
    assert [p for p, _ in got] == [P, 202609]                 # 新到舊
    assert dict(got)[P] == 3 and dict(got)[202609] == 1        # ANALYZE 後 reltuples 為實際筆數


def test_upload_status_reports_missing_chunks(conn, settings):
    data = b"x" * (settings.chunk_size + 10)
    chunks = [data[: settings.chunk_size], data[settings.chunk_size:]]
    sess = upload.create(conn, settings, filename="a.csv", total_bytes=len(data),
                         chunk_sha256=[hashlib.sha256(c).hexdigest() for c in chunks])
    uid = sess["upload_id"]
    assert upload.status(conn, settings, uid)["missing"] == [0, 1]
    upload.put_chunk(conn, settings, uid, 1, chunks[1])
    st = upload.status(conn, settings, uid)
    assert (st["status"], st["missing"]) == ("OPEN", [0])
    upload.put_chunk(conn, settings, uid, 0, chunks[0])
    upload.complete(conn, settings, uid)
    assert upload.status(conn, settings, uid) == {
        "upload_id": uid, "status": "COMPLETE", "missing": [], "total_chunks": 2, "total_bytes": len(data),
        "chunk_sha256": [hashlib.sha256(c).hexdigest() for c in chunks]}   # 前端續傳比對用
    with pytest.raises(upload.UploadError, match="格式錯誤"):
        upload.status(conn, settings, "../../etc")


# ---------------------------------------------------------------- 審查回歸（不需資料庫）
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.services.query.runner import _stable_order  # noqa: E402
from tests.conftest import small_dataset  # noqa: E402


@pytest.mark.parametrize("headers,blocked", [
    ({"origin": "https://evil.example"}, True),                       # 舊瀏覽器跨站表單
    ({"sec-fetch-site": "cross-site"}, True),
    ({"sec-fetch-site": "same-site"}, True),                          # 同網域其他子網站也不行
    ({"origin": "null"}, True),                                        # sandbox iframe
    ({"sec-fetch-site": "same-origin", "origin": "http://testserver"}, False),
    ({"origin": "http://testserver"}, False),
    ({}, False),                                                       # curl／批次腳本：非瀏覽器，不受 CSRF 影響
])
def test_cross_site_writes_blocked(headers, blocked):
    """只帶 query 參數的寫入（重試、取消、停用欄位）是 simple request，可被跨站表單觸發。"""
    c = TestClient(app)
    r = c.post("/api/query/00000000-0000-0000-0000-000000000000/cancel?x=1", headers=headers)
    if blocked:
        assert r.status_code == 403 and "跨站" in r.json()["error"]
    else:
        assert r.status_code != 403


def test_cross_site_get_allowed():
    r = TestClient(app).get("/api/health", headers={"origin": "https://evil.example"})
    assert r.status_code != 403                                       # 讀取不擋（CORS 本身阻止讀回應）


def test_export_form_bad_payload_is_400_json():
    r = TestClient(app).post("/api/query/export-form", data={"payload": '{"spec": 1, "encoding": "utf-16"}'},
                             headers={"sec-fetch-site": "same-origin"})
    assert r.status_code == 400 and r.json()["error"].startswith("匯出參數格式錯誤")


def test_stable_order_defaults_to_pk_only_when_missing():
    cat = {"a": small_dataset("a")}
    spec = {"from": "a", "select": ["a.note"]}
    assert _stable_order(spec, cat)["order"] == [["a.yyyymm", "asc", "nulls_last"], ["a.policy_no", "asc", "nulls_last"]]
    assert "order" not in spec                                         # 不修改原規格（匯出／儲存不受影響）
    given = {**spec, "order": [["a.note", "desc", "nulls_first"]]}
    assert _stable_order(given, cat) is given
