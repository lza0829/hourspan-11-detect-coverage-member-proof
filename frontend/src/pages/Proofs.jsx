import React, { useEffect, useState } from "react";
import { api } from "../api.js";

const KIND_LABEL = { changed: "变更", unchanged: "未变", skipped: "跳过" };

export default function Proofs() {
  const [rows, setRows] = useState([]);
  const [coverage, setCoverage] = useState(null);
  const [runId, setRunId] = useState("");
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  const reload = () =>
    api("/api/proofs").then(setRows).catch((e) => setErr(String(e.message || e)));

  const loadCoverage = (rid) => {
    const q = rid && rid.trim() ? `?run_id=${encodeURIComponent(rid.trim())}` : "";
    return api(`/api/detect-coverage${q}`)
      .then((p) => { setCoverage(p); setRunId(p.run_id); setErr(""); })
      .catch((e) => setErr(String(e.message || e)));
  };

  useEffect(() => { reload(); loadCoverage(""); }, []);

  const rebuild = async () => {
    setErr("");
    try {
      const r = await api("/api/proofs/rebuild", { method: "POST", body: "{}" });
      setMsg(JSON.stringify(r, null, 2));
      await reload();
    } catch (e) {
      setErr(String(e.message || e));
    }
  };

  return (
    <>
      <div className="topbar">
        <div>
          <h1>核证</h1>
          <p>
            当前镜 hist_id / row_hash 互证；检测覆盖核证：范围数、成员数、缺员列表、run_id、valid，
            成员集合与范围集合全等才可开段。
          </p>
        </div>
      </div>

      {coverage && (
        <div className="panel">
          <h2>
            检测覆盖核证 · <span className="mono">{coverage.run_id}</span>{" "}
            <span className={`badge ${coverage.valid ? "ok" : "bad"}`}>
              {coverage.valid ? "valid=true" : "valid=false 失败闭"}
            </span>
            <span className="muted" style={{ marginLeft: 8, fontSize: "0.8rem" }}>
              架构 {coverage.architecture}（乙：可落跑+失败闭）· 实时复算 {coverage.recomputed_valid ? "齐套" : "不齐"}
            </span>
          </h2>
          <div className="actions">
            <input
              value={runId}
              onChange={(e) => setRunId(e.target.value)}
              placeholder="detect_run_id（空=最近跑次）"
              style={{ minWidth: 240, padding: "0.4rem 0.6rem", borderRadius: 8, border: "1px solid var(--line)", background: "#0c2226", color: "var(--text)" }}
            />
            <button onClick={() => loadCoverage(runId)}>查覆盖核证</button>
          </div>
          <div className="grid-cards">
            <div className="card"><div className="label">范围数</div><div className="value">{coverage.scope_count}</div></div>
            <div className="card"><div className="label">成员数</div><div className="value">{coverage.member_count}</div></div>
            <div className="card"><div className="label">缺员</div><div className="value">{coverage.missing_snapshot_ids.length}</div></div>
            <div className="card"><div className="label">范围外</div><div className="value">{coverage.extraneous_snapshot_ids.length}</div></div>
            <div className="card"><div className="label">集合全等</div><div className="value">{coverage.sets_equal ? "是" : "否"}</div></div>
          </div>
          {coverage.missing_snapshot_ids.length > 0 && (
            <p className="error">缺员列表：<span className="mono">{coverage.missing_snapshot_ids.join(" · ")}</span></p>
          )}
          {coverage.extraneous_snapshot_ids.length > 0 && (
            <p className="error">范围外混入：<span className="mono">{coverage.extraneous_snapshot_ids.join(" · ")}</span></p>
          )}
          <table>
            <thead>
              <tr><th>成员快照</th><th>门店</th><th>kind</th><th>row_hash</th><th>明细</th></tr>
            </thead>
            <tbody>
              {coverage.members.map((m) => (
                <tr key={m.snapshot_id}>
                  <td className="mono">{m.snapshot_id}</td>
                  <td>{m.store_id}</td>
                  <td>
                    <span className={`badge ${m.kind === "changed" ? "ok" : m.kind === "skipped" ? "bad" : ""}`}>
                      {KIND_LABEL[m.kind] || m.kind}
                    </span>
                  </td>
                  <td className="mono">{m.row_hash || "—"}</td>
                  <td className="mono">{JSON.stringify(m.detail)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="panel">
        <div className="actions">
          <button onClick={rebuild}>重建镜互证</button>
          <button className="secondary" onClick={reload}>刷新</button>
        </div>
        {msg && <pre className="msg">{msg}</pre>}
        {err && <p className="error">{err}</p>}
      </div>
      <div className="panel">
        <h2>scd_proofs（当前镜 ↔ 历史行）</h2>
        <table>
          <thead>
            <tr>
              <th>id</th><th>门店</th><th>hist</th><th>mirror_hist</th><th>hash</th><th>valid</th><th>detail</th><th>时间</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td>{r.id}</td>
                <td>{r.store_id}</td>
                <td className="mono">{r.hist_id}</td>
                <td className="mono">{r.mirror_hist_id}</td>
                <td className="mono">{r.row_hash}</td>
                <td>
                  <span className={`badge ${r.valid ? "ok" : "bad"}`}>
                    {r.valid ? "通过" : "失败"}
                  </span>
                </td>
                <td className="mono">{r.detail_json}</td>
                <td>{r.created_at}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
