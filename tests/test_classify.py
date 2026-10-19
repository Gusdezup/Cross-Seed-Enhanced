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


def test_renvoi_sous_le_nom_local():
    from app.routing import local_name, norm, same_size
    assert local_name("Avatar.The.Way.Of.Water.2022.MULTi.VFF.1080p.mHD.x264.AC3.5.1-XSHD.mkv").endswith("-XSHD")
    assert norm("Avatar.La.Voie.de.l.Eau.2022") != norm("Avatar.The.Way.Of.Water.2022")
    assert same_size(9_000_000_000, 9_004_000_000)
    assert not same_size(9_000_000_000, 9_300_000_000)
    assert not same_size(0, 9_000_000_000)
