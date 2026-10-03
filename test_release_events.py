import os, tempfile
os.environ.setdefault("TELEGRAM_BOT_TOKEN","x")
os.environ.setdefault("TELEGRAM_CHAT_ID","x")
import release_events as r

def main():
    good={"schema":"release-event/v1","id":"software:github:o/r:v1","kind":"software","name":"Thing","version":"1.0","source":"clawbytes","url":"https://github.com/o/r/releases/tag/v1"}
    assert r.validate(good)==""
    assert r.canonical_id(good)==good["id"]
    assert "Thing 1.0" in r.format_event(good)
    bad=dict(good); bad["kind"]="paper"
    assert r.validate(bad)=="invalid kind"
    print("release event tests passed")
if __name__=="__main__": main()
