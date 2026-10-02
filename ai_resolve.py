import json
import os

from dotenv import load_dotenv

MAX_LINES = 400
CONTEXT = 30


def call_llm(prompt):
    # send one prompt to the model, return the reply text or None (never raises)
    try:
        # .env sits next to this file; real environment variables still win
        load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            print("AI: OPENAI_API_KEY is not set.")
            return None

        from openai import OpenAI

        client = OpenAI(api_key=key, base_url=os.environ.get("OPENAI_BASE_URL") or None)
        reply = client.chat.completions.create(
            model=os.environ.get("OPENAI_MODEL") or "gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
        )
        return reply.choices[0].message.content
    except Exception as e:
        # only the error type: some messages echo part of the key
        print(f"AI: model call failed ({type(e).__name__}).")
        return None


def trim_to_conflict(base, ours, theirs):
    # shrink long files to the part where ours and theirs differ, plus context
    o, t = ours.splitlines(), theirs.splitlines()
    if max(len(base.splitlines()), len(o), len(t)) <= MAX_LINES:
        return base, ours, theirs

    # lines that ours and theirs share at the start and end
    start = 0
    while start < min(len(o), len(t)) and o[start] == t[start]:
        start += 1
    end = 0
    while end < min(len(o), len(t)) - start and o[-1 - end] == t[-1 - end]:
        end += 1

    def cut(text):
        lines = text.splitlines()
        stop = len(lines) - end + CONTEXT
        return "\n".join(lines[max(0, start - CONTEXT) : stop])

    return cut(base), cut(ours), cut(theirs)


def explain_conflict(
    filename, base, ours, theirs, our_branch, their_branch, our_commit_msg, their_commit_msg
):
    # ask the model to explain one conflict and propose a merge, returns a dict
    base, ours, theirs = trim_to_conflict(base, ours, theirs)
    prompt = f"""You are helping resolve a git merge conflict in the file "{filename}".
(If the file text below is only an excerpt, "merged" should be the merged excerpt.)

Our branch: {our_branch}
Our commit message: {our_commit_msg}
Their branch: {their_branch}
Their commit message: {their_commit_msg}

=== BASE (common ancestor) ===
{base}
=== OURS ({our_branch}) ===
{ours}
=== THEIRS ({their_branch}) ===
{theirs}

Reply with JSON only, no markdown, with exactly these keys:
"explanation": what each side changed and why they clash, 3-5 lines
"risk_ours": what breaks if we keep only our side
"risk_theirs": what breaks if we keep only their side
"merged": the full proposed file content keeping both changes, no conflict markers
"confidence": "low", "medium" or "high"
"""
    text = call_llm(prompt)
    if text is None:
        return {"ok": False, "raw_text": None}

    # models often wrap JSON in ``` fences, strip them before parsing
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").removeprefix("json").strip()
    try:
        data = json.loads(cleaned)
        result = {
            key: data[key]
            for key in ("explanation", "risk_ours", "risk_theirs", "merged", "confidence")
        }
    except (ValueError, KeyError, TypeError):
        print("AI: could not read the model's reply as JSON.")
        return {"ok": False, "raw_text": text}

    result["ok"] = True
    return result
