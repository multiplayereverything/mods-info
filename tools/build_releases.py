#!/usr/bin/env python3
"""Builds releases.json for mods-info: the newest release of every team PC mod that follows its newest GitHub release.

The launcher reads this file (from the mods-info zip) so it does not have to ask api.github.com itself.
It copies the launcher engine's rule (old/src/core/mod/package.cpp findGithubAsset):
  releases: GET /repos/<repo>/releases?per_page=10, newest first; drafts are skipped, pre-releases are kept;
  asset: the first asset, in that order, whose name matches package.asset (case-insensitive, default *.zip);
  version: the tag without one leading v/V; sha256 from the asset's digest when it starts with "sha256:", else "";
  extras: GET /repos/<repo>/releases/tags/<tag>, first asset matching the extra's own glob. An extra that has its own
  pinned url is not looked up: the record's url, tag, asset and sha256 are echoed as they are (the record's values win).
Whose record it is is decided by the repository, never by a field in the record (the launcher's rule): the record's repo,
package.github and every extra's repo must belong to OWNER. Records with a pinned package.url are skipped.
Only the standard library; GITHUB_TOKEN is optional.

  python build_releases.py --root <mods-info checkout> [--dry-run]

--dry-run prints what would change and writes nothing. Exit 1 when something needs the owner's look (a first-seen hash
changed, GitHub failed for a mod with no earlier entry, a kept entry that has been stale for about a day, a record that
breaks the rules); the other mods are still written.

checkedAt moves only on a run where every mod resolved, so a broken token or an outage cannot look healthy.
"""
import argparse, datetime, fnmatch, http.client, json, os, re, sys, urllib.error, urllib.parse, urllib.request

API = "https://api.github.com"
OWNER = "multiplayereverything"
HEARTBEAT_HOURS = 20  # checkedAt is refreshed about daily when every mod resolved, even when nothing else changed
PLAIN = re.compile(r"^[A-Za-z0-9._+-]{1,120}$")  # the launcher's release_url_of accepts only plain path segments
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class Skip(Exception):
    """Leave this mod out on purpose (the launcher then asks GitHub); not a failure."""


def now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def http_fetch(path):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "mods-info-releases"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
    with urllib.request.urlopen(urllib.request.Request(API + path, headers=headers), timeout=30) as r:
        return json.load(r)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def owner_of(repo):
    return str(repo or "").split("/")[0].lower()


def is_ours(repo):
    return owner_of(repo) == OWNER


def load_records(root):
    """The records listed in records/index.json (the only folder with mods), or [] when it lists none."""
    idx = read_json(os.path.join(root, "records", "index.json"))
    out = []
    for i in (idx or {}).get("mods", []):
        r = read_json(os.path.join(root, "records", i + ".json"))
        if r:
            out.append(r)
    return out


def team_rule(rec):
    """None when the record is not ours (developers' records live in records/ too: skip them quietly), or an error text when
    it is ours but breaks the rules, or True when it is ours and fine."""
    pkg = (rec.get("mod") or {}).get("package") or {}
    repo = rec.get("repo")
    if not is_ours(repo):
        return None
    if not pkg.get("github") or pkg.get("url"):
        return None  # a Flash game or a pinned download: nothing to look up
    if str(pkg["github"]).lower() != str(repo).lower():
        return f"{rec.get('id')}: package.github {pkg['github']!r} is not the record's repo {repo!r}"
    for x in pkg.get("extras") or []:
        if x.get("github") and not is_ours(x["github"]):
            return f"{rec.get('id')}: extra {x.get('dir')!r} comes from {x['github']!r}, not {OWNER}/..."
    return True


def pick_asset(assets, glob, what):
    if glob is not None and "[" in glob:
        raise LookupError(f"{what}: the glob {glob!r} has [ ], which the script will not guess about")
    g = (glob or "*.zip").lower()
    for a in assets:
        if a.get("name") and a.get("browser_download_url") and fnmatch.fnmatchcase(a["name"].lower(), g):
            return a
    return None


def plain_entry(repo, tag, asset):
    name = asset["name"]
    if not PLAIN.match(tag) or not PLAIN.match(name):
        raise Skip(f"{repo}: {tag!r} / {name!r} has characters the launcher's URL check does not allow")
    url = f"https://github.com/{repo}/releases/download/{tag}/{name}"
    # GitHub spells the repository as it was created; compare without case but publish the record's own spelling,
    # which is what the launcher checks the url against.
    if asset["browser_download_url"].lower() != url.lower():
        raise ValueError(f"download link for {name} is not the standard shape: {asset['browser_download_url']}")
    digest = (asset.get("digest") or "").lower()
    sha = digest[len("sha256:"):] if digest.startswith("sha256:") else ""
    return {"repo": repo, "tag": tag, "asset": name, "url": url, "sha256": sha if HEX64.match(sha) else ""}


def resolve(rec, fetch):
    pkg = rec["mod"]["package"]
    repo = pkg["github"]
    rels = [r for r in fetch(f"/repos/{repo}/releases?per_page=10") if not r.get("draft")]
    found = None
    for rel in rels:
        a = pick_asset(rel.get("assets", []), pkg.get("asset"), repo)
        if a:
            found = (rel["tag_name"], a)
            break
    if not found:
        raise LookupError(f"{repo}: none of the newest 10 releases has a file matching {pkg.get('asset') or '*.zip'}")
    tag, a = found
    e = plain_entry(repo, tag, a)
    entry = {"repo": repo, "tag": tag, "version": tag[1:] if tag[:1] in ("v", "V") else tag,
             "asset": e["asset"], "url": e["url"], "sha256": e["sha256"], "extras": []}
    for x in pkg.get("extras") or []:
        xr = x.get("github") or repo
        if x.get("url"):  # the record pins it: its values win, nothing to look up
            entry["extras"].append({"dir": x.get("dir", ""), "repo": xr, "tag": x.get("tag", ""), "asset": x.get("asset", ""),
                                    "url": x["url"], "sha256": str(x.get("sha256") or "")})
            continue
        if not x.get("tag") or not x.get("asset"):
            raise LookupError(f"{rec['id']}: extra {x.get('dir')!r} has no tag or no asset")
        rel = fetch(f"/repos/{xr}/releases/tags/{urllib.parse.quote(x['tag'])}")
        xa = pick_asset(rel.get("assets", []), x["asset"], xr)
        if not xa:
            raise LookupError(f"{xr}@{x['tag']}: no file matching {x['asset']}")
        entry["extras"].append({"dir": x.get("dir", ""), **plain_entry(xr, x["tag"], xa)})
    return entry


def apply_first_seen(entry, seen, problems):
    """Keeps the first sha256 seen for each repo+tag+asset. A changed hash is never published: the old one stays."""
    for t in [entry] + entry["extras"]:
        if not t["tag"]:
            continue
        k = f"{t['repo']}|{t['tag']}|{t['asset']}"
        if not t["sha256"]:
            if k in seen:  # GitHub stopped giving a digest for a file we have seen: keep the first one, never publish ""
                t["sha256"] = seen[k]
            continue
        if k not in seen:
            seen[k] = t["sha256"]
        elif seen[k] != t["sha256"]:
            problems.append(f"HASH CHANGED for {k}: first seen {seen[k]}, GitHub now says {t['sha256']}. "
                            f"Still serving the first. If the new file is the real one, remove that key from releases-seen.json.")
            t["sha256"] = seen[k]


def build(root, fetch=http_fetch, when=None):
    when = when or now()
    old = read_json(os.path.join(root, "releases.json"), {}) or {}
    old_mods = old.get("mods", {})
    seen = read_json(os.path.join(root, "releases-seen.json"), {}) or {}
    records = load_records(root)
    mods, problems, notes, failed = {}, [], [], []
    if not records:
        # Never publish an empty list because records/ could not be read: leave the file as it is and say so.
        return old or {"format": 1, "checkedAt": None, "mods": {}}, read_json(os.path.join(root, "releases-seen.json"), {}) or {}, False, False, \
            ["records/index.json lists no mods; releases.json was left as it is"], []
    for rec in records:
        rule = team_rule(rec)
        if rule is not True:
            if rule:
                problems.append(rule)
            continue
        rid = rec["id"]
        try:
            e = resolve(rec, fetch)
        except Skip as s:
            notes.append(f"{rid}: left out ({s})")
            continue
        except (urllib.error.URLError, http.client.HTTPException, LookupError, ValueError, KeyError, OSError) as err:
            failed.append(rid)
            if rid in old_mods:
                mods[rid] = old_mods[rid]
                notes.append(f"{rid}: kept the earlier entry ({err})")
            else:
                problems.append(f"{rid}: no entry ({err})")
            continue
        apply_first_seen(e, seen, problems)
        mods[rid] = e
    mods = dict(sorted(mods.items()))
    changed = mods != old_mods
    last = old.get("checkedAt")
    try:
        age_h = (when - datetime.datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)).total_seconds() / 3600
    except (TypeError, ValueError):
        age_h = 1e9
    all_ok = not failed
    beat = all_ok and age_h >= HEARTBEAT_HOURS
    stale_kept = [i for i in failed if i in old_mods] and age_h >= HEARTBEAT_HOURS
    if stale_kept:
        problems.append(f"checkedAt is {age_h:.0f} hours old because {', '.join(failed)} could not be read; the launcher will soon stop trusting the file")
    checked = iso(when) if (all_ok and (changed or beat)) else last
    return {"format": 1, "checkedAt": checked, "mods": mods}, seen, changed, beat, problems, notes


def dump(o):
    return json.dumps(o, indent=2, ensure_ascii=False) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    new, seen, changed, beat, problems, notes = build(a.root)
    for n in notes:
        print("note:", n)
    for p in problems:
        print("PROBLEM:", p)
    print(f"{len(new['mods'])} mods; releases.json would {'change' if changed else 'not change'}; heartbeat {'due' if beat else 'not due'}")
    for i, e in new["mods"].items():
        print(f"  {i}: {e['tag']} {e['asset']} sha256={'yes' if e['sha256'] else 'NO'}; extras: " +
              ", ".join(f"{x['dir']}={x['tag']} sha256={'yes' if x['sha256'] else 'NO'}" for x in e["extras"]))
    old_seen = read_json(os.path.join(a.root, "releases-seen.json"), {}) or {}
    write = changed or beat or seen != old_seen
    if a.dry_run:
        print("dry run: nothing written")
    elif write:
        with open(os.path.join(a.root, "releases.json"), "w", encoding="utf-8", newline="\n") as f:
            f.write(dump(new))
        with open(os.path.join(a.root, "releases-seen.json"), "w", encoding="utf-8", newline="\n") as f:
            f.write(dump(dict(sorted(seen.items()))))
        print("written")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
