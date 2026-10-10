"""單一學生畢業審查明細：可列印（或另存 PDF）的獨立 HTML，附規則版本與審查時間。"""
from __future__ import annotations

from datetime import datetime
from html import escape

STATUS_TEXT = {"PASS": "符合（Pass）", "WARNING": "需確認（Warning）", "FAIL": "不符合（Fail）",
               "MANUAL_REVIEW": "需人工判斷（Manual Review）"}

STYLE = """
body { margin: 0; background: #fff; color: #1c2230; font: 14px/1.6 "Noto Sans TC", "Microsoft JhengHei", sans-serif; }
main { max-width: 860px; margin: 0 auto; padding: 24px 16px; }
h1 { font-size: 20px; margin: 0 0 4px; } h2 { font-size: 15px; margin: 22px 0 8px; border-bottom: 2px solid #1c2230; }
.meta { color: #555; font-size: 12.5px; }
.verdict { display: inline-block; margin: 10px 0; padding: 4px 12px; border: 2px solid; border-radius: 6px; font-weight: 700; }
.PASS { color: #1f6f3f; } .FAIL { color: #b02a2a; } .MANUAL_REVIEW { color: #5b3aa8; } .WARNING { color: #946010; }
table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
th, td { border: 1px solid #ccc; padding: 4px 6px; text-align: left; vertical-align: top; }
th { background: #f2f3f5; white-space: nowrap; }
td.state { white-space: nowrap; }
td.num { text-align: right; white-space: nowrap; }
.cite { color: #666; font-size: 11.5px; }
.note { color: #555; font-size: 12px; margin-top: 18px; }
.actions { margin-bottom: 12px; }
@media print { .actions { display: none; } main { padding: 0; } h2 { break-after: avoid; } tr { break-inside: avoid; } }
"""


def render(data: dict, program: str, cohort: int, rule_version: str, audited_at: datetime | None = None) -> str:
    """data 為 AcademicTwinService.student() 的結果。"""
    e = lambda v: escape(str(v))
    stamp = (audited_at or datetime.now()).isoformat(timespec="seconds")
    status = data["status"]

    def mark(r: dict) -> str:
        return "✔ 符合" if r["satisfied"] else "？ 待確認" if r.get("pending") else "✘ 未符合"

    req_rows = "".join(
        f"<tr><td>{e(r['id'])}</td><td>{e(r['title'])}"
        + (f"<div class='cite'>缺：{'、'.join(e(c['code'] + ' ' + c['name']) for c in r['missing'])}</div>"
           if r["missing"] and not r["satisfied"] else "")
        + f"<div class='cite'>依據：{e(r['citation'])}</div></td>"
        f"<td class='num'>{e(r['have'])} / {e(r['need'])}{('<br>' + e(r['detail'])) if r.get('detail') else ''}</td>"
        f"<td class='state'>{mark(r)}</td></tr>"
        for r in data["requirements"]
    )
    breakdown = "".join(f"<tr><td>{e(k)}</td><td class='num'>{e(v)}</td></tr>" for k, v in data["credit_breakdown"].items())

    issues = (
        [f"需人工判斷：{e(r['code'])} {e(r['name'])}（{e(r['term'])}，類別「{e(r['category'])}」）" for r in data["issues"]["review"]]
        + [f"不及格未重修通過：{e(r['code'])} {e(r['name'])}（{e(r['term'])}，{e(r['grade'])} 分）" for r in data["issues"]["failed"]]
        + [f"本系選修未核對：{e(r['code'])} {e(r['name'])}（{e(r['term'])}）" for r in data["issues"]["dept_unverified"]]
    )
    reviews = "".join(
        f"<tr><td>{e(r['code'])} {e(r['name'])}（{e(r['term'])}）</td><td>{e(r['label'])}</td>"
        f"<td>{e(r['reviewer'])}</td><td>{e(r['decided_at'])}</td><td>{e(r['note'])}"
        + ("<div class='cite'>決定時的規則版本與現行不同，建議再確認</div>" if r["outdated"] else "") + "</td></tr>"
        for r in data["reviews"]
    )
    records = "".join(
        f"<tr><td>{e(r['term'])}</td><td>{e(r['code'])}</td><td>{e(r['name'])}</td><td class='num'>{e(r['credits'])}</td>"
        f"<td class='num'>{e(r['grade'])}</td><td>{e(r['category'])}</td></tr>"
        for r in data["records"]
    )

    return f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>畢業審查明細 {e(data['id'])}</title><style>{STYLE}</style></head>
<body><main>
<div class="actions"><button onclick="window.print()">列印／另存 PDF</button></div>
<h1>畢業資格審查明細</h1>
<div class="meta">{e(program)}・{e(cohort)} 學年度入學・學號 {e(data['id'])}</div>
<div class="meta">規則版本 {e(rule_version)}・審查時間 {e(stamp)}</div>
<div class="verdict {e(status)}">判定：{e(STATUS_TEXT.get(status, status))}　總學分 {e(data['total_credits'])}</div>

<h2>畢業條件</h2>
<table><thead><tr><th>編號</th><th>條件</th><th>已達成</th><th>結果</th></tr></thead><tbody>{req_rows}</tbody></table>

<h2>學分採計</h2>
<table><thead><tr><th>項目</th><th>學分</th></tr></thead><tbody>{breakdown}
<tr><th>合計</th><th class="num">{e(data['total_credits'])}</th></tr></tbody></table>

<h2>需要注意</h2>
{"<ul>" + "".join(f"<li>{i}</li>" for i in issues) + "</ul>" if issues else "<p>無</p>"}

<h2>人工覆核紀錄</h2>
{f"<table><thead><tr><th>紀錄</th><th>決定</th><th>覆核人</th><th>時間</th><th>備註</th></tr></thead><tbody>{reviews}</tbody></table>" if reviews else "<p>無</p>"}

<h2>修課紀錄（{len(data['records'])} 筆）</h2>
<table><thead><tr><th>學期</th><th>課號</th><th>課名</th><th>學分</th><th>成績</th><th>類別</th></tr></thead><tbody>{records}</tbody></table>

<p class="note">本明細由規則引擎依規則庫自動產生，規則庫仍有待系辦確認的假設；最終畢業資格以系辦審核為準。</p>
</main></body></html>
"""
