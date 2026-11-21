import React, { useEffect, useState } from "react";
import { api } from "../api.js";

const KIND_LABEL = { changed: "变更", unchanged: "未变", skipped: "跳过" };

export default function Detect() {
  const [runs, setRuns] = useState([]);
  const [proof, setProof] = useState(null);
  const [batchId, setBatchId] = useState("");
  const [proofRunId, setProofRunId] = useState("");
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  const reload = () =>
    api("/api/detect-runs")
      .then((rs) => {
        setRuns(rs);
        const target = proofRunId || rs[0]?.id;
        if (target) return loadProof(target);
        setProof(null);
      })
      .catch((e) => setErr(String(e.message || e)));

  const loadProof = (rid) =>
    api(`/api/detect-coverage?run_id=${encodeURIComponent(rid)}`)
      .then((p) => { setProof(p); setProofRunId(rid); })
      .catch((e) => setErr(String(e.message || e)));

  useEffect(() => { reload(); }, []);

  const run = async () => {
    setErr("");
    try {
      const body = batchId.trim() ? { batch_id: batchId.trim() } : {};
      const r = await api("/api/detect/run", { method: "POST", body: JSON.stringify(body) });
      setMsg(JSON.stringify(r, null, 2));
      setProofRunId(r.detect_run_id);
      await reload();
    } catch (e) {
      setErr(String(e.message || e));
    }
  };

  return (
    <>
      <div className="topbar">
        <div>
          <h1>变更检测 · 覆盖核证</h1>
          <p>
            架构乙（可落跑+失败闭）：约定范围内每条入档快照都落成员
            changed/unchanged/skipped；成员集合须与范围集合全等，缺员则 valid=false 且禁止开段。
          </p>
        </div>
      </div>
      <div className="panel">
        <div className="actions">
          <input
            value={batchId}
            onChange={(e) => setBatchId(e.target.value)}
            placeholder="可选 batch_id，空=各店最新快照"
            style={{ minWidth: 280, padding: "0.4rem 0.6rem", borderRadius: 8, border: "1px solid var(--line)", background: "#0c2226", color: "var(--text)" }}
          />
          <button onClick={run}>跑检测</button>
          <button className="secondary" onClick={reload}>刷新</button>
        </div>
        {msg && <pre className="msg">{msg}</pre>}
        {err && <p className="error">{err}</p>}
      </div>

      {proof && (
        <div className="panel">
          <h2>
            覆盖核证 · <span className="mono">{proof.run_id}</span>{" "}
            <span className={`badge ${proof.valid ? "ok" : "bad"}`}>
              {proof.valid ? "valid=true 齐套" : "valid=false 失败闭"}
            </span>
          </h2>
          <div className="grid-cards">
            <div className="card"><div className="label">范围数</div><div className="value">{proof.scope_count}</div></div>
            <div className="card"><div className="label">成员数</div><div className="value">{proof.member_count}</div></div>
            <div className="card"><div className="label">changed</div><div className="value">{proof.kind_counts.changed}</div></div>
            <div className="card"><div className="label">unchanged</div><div className="value">{proof.kind_counts.unchanged}</div></div>
            <div className="card"><div className="label">skipped</div><div className="value">{proof.kind_counts.skipped}</div></div>
            <div className="card"><div className="label">集合全等</div><div className="value">{proof.sets_equal ? "是" : "否"}</div></div>
          </div>
          {proof.missing_snapshot_ids?.length > 0 && (
            <p className="error">
              缺员列表（{proof.missing_snapshot_ids.length}）：
              <span className="mono">{proof.missing_snapshot_ids.join(" · ")}</span>
            </p>
          )}
          {proof.extraneous_snapshot_ids?.length > 0 && (
            <p className="error">
              范围外混入（{proof.extraneous_snapshot_ids.length}）：
              <span className="mono">{proof.extraneous_snapshot_ids.join(" · ")}</span>
            </p>
          )}
          <table>
            <thead>
              <tr><th>成员快照</th><th>门店</th><th>kind</th><th>row_hash</th><th>明细</th></tr>
            </thead>
            <tbody>
              {proof.members.map((m) => (
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
        <h2>检测跑次（跑次头定稿，失败不可翻案）</h2>
        <table>
          <thead>
            <tr><th>跑次</th><th>批</th><th>时间</th><th>valid</th><th>范围/成员/缺员</th><th>变更数</th><th>核证</th></tr>
          </thead>
          <tbody>
            {runs.map((r) => (
              <tr key={r.id}>
                <td className="mono">{r.id}</td>
                <td className="mono">{r.batch_id || "—"}</td>
                <td>{r.created_at}</td>
                <td>
                  <span className={`badge ${r.valid ? "ok" : "bad"}`}>
                    {r.valid ? "true" : "false"}
                  </span>
                </td>
                <td className="mono">{r.scope_count} / {r.member_count} / {r.missing_count}</td>
                <td>{r.changed_count}</td>
                <td><button className="secondary" onClick={() => loadProof(r.id)}>查核证</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
