from app.routing import classify

LINE = ("2026-10-19 00:22:11.646 info: [announce] Found Black.Clover.S02E01.VOSTFR.1080p.WEBRiP.x265-KAF.mkv "
        "[86d87620...] on C411 by MATCH from torrentClient (Black.Clover.S02E01.VOSTFR.1080p.WEBRiP.x265-KAF.mkv "
        "[933eff38...@192.168.1.20:8081]) - injected")


def _c(title, line=LINE, status=200):
    return classify(title, status, line.splitlines(), line)


def test_titre_sans_extension_reconnu():
    assert _c("Black.Clover.S02E01.VOSTFR.1080p.WEBRiP.x265-KAF") == "injected"


def test_titre_avec_extension_reconnu():
    assert _c("Black.Clover.S02E01.VOSTFR.1080p.WEBRiP.x265-KAF.mkv") == "injected"


def test_titre_plus_court_pas_confondu():
    assert _c("Black.Clover.S02E01.VOSTFR.1080p.WEBRiP.x265") == "matched"
