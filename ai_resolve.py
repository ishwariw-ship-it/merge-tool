import ast
import builtins
import json
import os

from dotenv import load_dotenv

MAX_LINES = 2000
CONTEXT = 30
LANGUAGE_TAGS = {"python", "py", "javascript", "js", "typescript", "ts", "json", "text", "bash", "sh"}


def call_llm(prompt, json_mode=True):
    # send one prompt to the model, return the reply text or None (never raises)
    # json_mode=False asks for a plain text reply
    try:
        # .env sits next to this file; real environment variables still win
        load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            print("AI: OPENAI_API_KEY is not set.")
            return None

        from openai import OpenAI

        client = OpenAI(
            api_key=key,
            base_url=os.environ.get("OPENAI_BASE_URL") or None,
            timeout=60,
            max_retries=1,
        )
        reply = client.chat.completions.create(
            model=os.environ.get("OPENAI_MODEL") or "gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=16000,
            **({"response_format": {"type": "json_object"}} if json_mode else {}),
        )
        choice = reply.choices[0]
        if choice.finish_reason == "length":
            print("AI: reply was cut off (file too long)")
            return None
        return choice.message.content
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


def looks_escaped(text):
    # one line that holds literal backslash-n instead of real newlines
    return len(text.strip().splitlines()) <= 1 and "\\n" in text


def undefined_names(tree):
    # names that are read but never defined, imported or assigned (builtins ignored)
    known = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__package__", "__spec__"}
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            known.add(node.name)
        elif isinstance(node, ast.Import):
            known.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            known.update(a.asname or a.name for a in node.names)
        elif isinstance(node, ast.arg):
            known.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            known.add(node.name)
        elif isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load):
                used.add(node.id)
            else:
                known.add(node.id)
    return sorted(used - known)


def check_merged(filename, text):
    # quick sanity checks on a proposed file, returns a list of problem strings
    problems = []

    if filename.endswith(".py"):
        try:
            tree = ast.parse(text)
            compile(text, filename, "exec")
        except (SyntaxError, ValueError) as e:
            problems.append(f"does not compile: {e}")
        else:
            has_star = any(
                isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names)
                for n in ast.walk(tree)
            )
            if has_star:
                # names may come from the star import, so don't guess
                problems.append("has star import, undefined-name check is unreliable")
            else:
                names = undefined_names(tree)
                if names:
                    problems.append("undefined names: " + ", ".join(names))

    if "<<<<<<<" in text or ">>>>>>>" in text:
        problems.append("still has conflict markers")
    if looks_escaped(text):
        problems.append("looks like escaped text")
    return problems


def explain_conflict(
    filename, base, ours, theirs, our_branch, their_branch, our_commit_msg, their_commit_msg
):
    # ask the model to explain one conflict and propose a merge, returns a dict
    base, ours, theirs = trim_to_conflict(base, ours, theirs)
    # no base means both sides added the file
    add_add = not base.strip()

    prompt = f"""You are helping resolve a git merge conflict in the file "{filename}".
(If the file text below is only an excerpt, "merged" should be the merged excerpt.)

Our branch: {our_branch}
Our commit message: {our_commit_msg}
Their branch: {their_branch}
Their commit message: {their_commit_msg}

"""
    if add_add:
        prompt += "Both branches added this file, so there is no common ancestor.\n\n"
    else:
        prompt += f"=== BASE (common ancestor) ===\n{base}\n"
    prompt += f"""=== OURS ({our_branch}) ===
{ours}
=== THEIRS ({their_branch}) ===
{theirs}

Reply with JSON only. Never wrap the JSON in markdown fences. Put the merged file
content in "merged" as a normal JSON string (escape newlines and quotes).
Use exactly these keys:
"explanation": what each side changed and why they clash, 3-5 lines
"risk_ours": what breaks if we keep only our side
"risk_theirs": what breaks if we keep only their side
"""
    if add_add:
        prompt += """"recommend": "ours" or "theirs", whichever file should be the base of the result
"port_from_other": a short list of strings, things worth bringing over from the other side
"merged": the full correct file ONLY if you are confident you can write it with nothing
  missing or invented (every name defined, every import present); otherwise an empty string
"confidence": "low", "medium" or "high"
"""
    else:
        prompt += """"merged": the full proposed file content keeping both changes, no conflict markers
"confidence": "low", "medium" or "high"
"""

    # try the call twice: a bad reply is often fine the second time
    for attempt in (1, 2):
        text = call_llm(prompt)
        if text is None:
            return {"ok": False, "raw_text": None}

        # models often wrap JSON in ``` fences, strip them before parsing
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`").removeprefix("json").strip()
        try:
            try:
                data = json.loads(cleaned)
            except ValueError:
                # last try: keep only the text from the first { to the last }
                data = json.loads(cleaned[cleaned.index("{") : cleaned.rindex("}") + 1])
            # a missing key becomes an empty string instead of a failure
            result = {
                key: data.get(key, "")
                for key in ("explanation", "risk_ours", "risk_theirs", "merged", "confidence")
            }
        except (ValueError, AttributeError) as e:
            print(f"AI: could not read the model's reply as JSON ({type(e).__name__}: {e}).")
            continue

        # recommend / port_from_other only mean something for add/add conflicts
        recommend = data.get("recommend", "") if add_add else ""
        result["recommend"] = recommend if recommend in ("ours", "theirs") else ""
        port = data.get("port_from_other", []) if add_add else []
        result["port_from_other"] = port if isinstance(port, list) else []

        merged = result["merged"] if isinstance(result["merged"], str) else ""
        if looks_escaped(merged):
            merged = merged.replace("\\n", "\n")
        # a leaked language tag or ``` fence on the first line is not file content
        first, _, rest = merged.partition("\n")
        if first.strip().lower() in LANGUAGE_TAGS or first.startswith("```"):
            merged = rest
        result["merged"] = merged

        # an empty merged (model not confident) has nothing to check
        # (check_merged still flags escaped text if the conversion left it that way)
        result["checks"] = check_merged(filename, merged) if merged else []

        result["ok"] = True
        return result

    return {"ok": False, "raw_text": text}


def ask_question(question, filename, base, ours, theirs, our_branch, their_branch):
    # answer one free-form question about a conflict, returns plain text or None
    prompt = f"""You are helping someone resolve a git merge conflict in the file "{filename}".

=== BASE (common ancestor) ===
{base if base.strip() else "(none: both branches added this file)"}
=== OURS ({our_branch}) ===
{ours}
=== THEIRS ({their_branch}) ===
{theirs}

Question: {question}

Answer in plain English, in at most 8 lines. Be concrete: name the lines or
functions involved where you can. Reply with plain text only, not JSON.
"""
    text = call_llm(prompt, json_mode=False)
    return text.strip() if text else None
