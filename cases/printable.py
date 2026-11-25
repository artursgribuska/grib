"""Printable case file: one self-contained HTML page per case, ready to save as PDF.

Compliance teams file cases as documents. This renders the same structured case (and its
evidence bundle) as a clean A4/Letter page: header facts, summary, timeline, evidence with
the engine's score breakdown, innocent explanations, rating, next steps, open questions,
data limitations, and the automated-check footer. No external assets; all text escaped;
wallets only ever appear truncated (they are truncated in the bundle already).

Open the .html in any browser and use Print → Save as PDF.
"""

from __future__ import annotations

import html

e = lambda x: html.escape(str(x), quote=True)

CSS = """
@page{size:A4;margin:18mm 16mm}
*{box-sizing:border-box}body{margin:0;font:10.5pt/1.45 Georgia,'Times New Roman',serif;color:#111;background:#fff}
main{max-width:180mm;margin:0 auto;padding:12px}
h1{font:600 16pt/1.25 system-ui,-apple-system,'Segoe UI',sans-serif;margin:0 0 4px}
h2{font:600 11.5pt system-ui,-apple-system,'Segoe UI',sans-serif;margin:18px 0 6px;border-bottom:1px solid #ccc;padding-bottom:2px}
.disc{font:9pt system-ui,sans-serif;color:#444;border-left:3px solid #888;padding:4px 8px;margin:6px 0 12px}
table{border-collapse:collapse;width:100%;font:9.5pt system-ui,-apple-system,'Segoe UI',sans-serif}
th,td{border-bottom:1px solid #ddd;padding:3px 6px;text-align:left;vertical-align:top}
th{color:#555;font-weight:600}.num{text-align:right;white-space:nowrap}
.facts td:first-child{color:#555;width:32%}
.rating{font:600 11pt system-ui,sans-serif}.high{color:#a3261b}.medium{color:#8a5a00}.low{color:#1f6b3a}
ul,ol{margin:4px 0 4px 18px;padding:0}li{margin:2px 0}
.foot{margin-top:18px;font:8.5pt system-ui,sans-serif;color:#555;border-top:1px solid #ccc;padding-top:6px}
section{break-inside:avoid-page}tr{break-inside:avoid}
@media screen{body{background:#f3f3f1}main{background:#fff;margin:16px auto;padding:24px 28px;box-shadow:0 1px 4px #0002}}
"""


def render_html(bundle: dict, case: dict, checks: dict) -> str:
    """checks: {"passed": bool, "unmatched": [...], "language": [...], "model": str}"""
    m, a, p, s = bundle["market"], bundle["account"], bundle["position"], bundle["score"]
    rr = case["risk_rating"]
    lvl = rr["level"].lower()
    li = lambda xs: "".join(f"<li>{e(x)}</li>" for x in xs)
    facts = [
        ("Case ID", bundle["case_id"]),
        ("Account", f"{a['display_id']} (pseudonymous)"),
        ("Market", f"{m['question']} ({m['category'].replace('_', ' ')})"),
        ("Outcome bought / winner", f"{p['outcome_bought']} / {m['winning_outcome']}"),
        ("Position", f"${p['cost_usd']:,.0f} at avg {p['average_price']:.2f} → paid ${p['payout_at_resolution_usd']:,.0f}"),
        ("Outcome public (est.) / resolved", f"{m.get('outcome_became_public_at', '—')} / {m.get('officially_resolved_at', '—')}"),
        ("Engine score", f"{s['total']:.0f} / 100"),
        ("Generated", bundle.get("generated_at", "")),
    ]
    points = "".join(f"<tr><td>{e(k)}</td><td class=num>{v:+.1f}</td></tr>" for k, v in s["points_by_signal"].items())
    checks_txt = ("Number check: all numbers traced to the evidence bundle. " if not checks.get("unmatched")
                  else f"Number check: NOT traced — {', '.join(map(str, checks['unmatched']))}. ")
    checks_txt += ("Language check: passed." if not checks.get("language")
                   else f"Language check: {'; '.join(checks['language'])}.")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(bundle['case_id'])} case file</title>
<style>{CSS}</style></head><body><main>
<h1>Case file: {e(m['question'])}</h1>
<div class="disc">{e(bundle['disclaimer'])}</div>
<section><table class="facts">{"".join(f"<tr><td>{e(k)}</td><td>{e(v)}</td></tr>" for k, v in facts)}
<tr><td>Risk rating</td><td class="rating {e(lvl)}">{e(rr['level'])} (confidence: {e(rr['confidence'])})</td></tr></table></section>
<section><h2>Summary</h2><p>{e(case['summary'])}</p></section>
<section><h2>Timeline</h2><table><thead><tr><th>Time</th><th>Event</th></tr></thead><tbody>
{"".join(f"<tr><td style='white-space:nowrap'>{e(t['time'])}</td><td>{e(t['event'])}</td></tr>" for t in case['timeline'])}
</tbody></table></section>
<section><h2>Evidence</h2><ul>{"".join(f"<li><b>{e(x['signal'])}</b> ({e(x['weight'])}): {e(x['finding'])}</li>" for x in case['evidence'])}</ul>
<table style="width:60%;margin-top:6px"><thead><tr><th>Engine score breakdown</th><th class=num>Points</th></tr></thead>
<tbody>{points}<tr><td><b>Total</b></td><td class=num><b>{s['total']:.1f}</b></td></tr></tbody></table></section>
<section><h2>Innocent explanations considered</h2><ul>
{"".join(f"<li><b>{e(i['explanation'])}</b> — <i>{e(i['fits'])}</i>. {e(i['assessment'])}</li>" for i in case['innocent_explanations'])}
</ul></section>
<section><h2>Risk rating</h2><p><span class="rating {e(lvl)}">{e(rr['level'])}</span> (confidence: {e(rr['confidence'])}).
{e(rr['rationale'])}</p></section>
<section><h2>Recommended next steps</h2><ol>{li(case['next_steps'])}</ol></section>
<section><h2>Open questions</h2><ul>{li(case['open_questions'])}</ul></section>
<section><h2>Data limitations</h2><ul>{li(bundle.get('data_limitations', []))}</ul></section>
<div class="foot">Drafted by {e(checks.get('model', ''))} from the evidence bundle only; requires review by a compliance
analyst. {e(checks_txt)}</div>
</main></body></html>"""
