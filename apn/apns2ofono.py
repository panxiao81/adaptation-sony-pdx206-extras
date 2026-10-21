#!/usr/bin/env python3
"""Convert an Android apns-conf.xml into the serviceproviders.xml dialect that
oFono's provisioning reads (mobile-broadband-provider-info format 2.0 plus
oFono's <usage type="ims"> and <protocol type=...> extensions).

usage: apns2ofono.py APNS_CONF CARRIER_LIST MCC_ISO_JSON OUT
       apns2ofono.py --check OUT
  APNS_CONF:    the only source of APN data (Sony's stock apns-conf.xml).
  CARRIER_LIST: Android's carrier-ID database from the same firmware
                (TelephonyProvider assets/carrier_list.textpb), for names:
                apns-conf names APNs ("spモード"), not operators.
  MCC_ISO_JSON: MCC -> ISO country (mcc-iso.json, from AOSP MccTable.java);
                the output format needs a country, the source has none.

Readers of the output and what they need:
  * oFono (sailfish_provision) ranks APNs per type by how well the provider
    and APN names match the SIM's SPN, then primary="true", then file order.
  * libnma / GNOME Settings looks up the first provider for the SIM's MCCMNC
    and lists its APNs, hiding only ones with "mms" in the APN or name.
  * NetworkManager's gsm.auto-config takes the first internet APN.
So: one primary provider per operator holding all its own APNs (usage per
APN, named "Internet"/"MMS" like oFono's defaults), MVNO/brand presets as
separate non-primary providers named after their brand (or SPN when the
entry is SPN-matched), operators first, duplicates dropped. IMS APNs that
are just "ims" are left out: oFono provisions that default itself, and
Settings would offer them as data APNs.
"""
import collections, json, os, re, sys, xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

USAGE = (("default", "internet"), ("mms", "mms"), ("ims", "ims"))
NAME = {"internet": "Internet", "mms": "MMS", "ims": "IMS"}
PROTO = {"IP": "ip", "IPV6": "ipv6", "IPV4V6": "ipv4v6"}
AUTH = {"0": "none", "1": "pap", "2": "chap", "3": "any"}


def mcc_countries(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)["mcc"]


class Carriers:
    """carrier_list.textpb: canonical carrier names, the networks they use,
    the APNs they prefer and how MVNOs are told apart (gid1)."""

    def __init__(self, path):
        self.all = []
        text = open(path, encoding="utf-8").read()
        for block in re.findall(r"^carrier_id \{\n(.*?)^\}", text, re.S | re.M):
            field = lambda k: re.findall(r'^\s*%s: "?([^"\n]*)"?$' % k, block, re.M)
            self.all.append({
                "id": int(field("canonical_id")[0]),
                "name": (field("carrier_name") or [None])[0],
                "plmns": set(field("mccmnc_tuple")),
                "apns": {a.lower() for a in field("preferred_apn")},
                "gid1": {g.lower() for g in field("gid1")},
                "parent": bool(field("parent_canonical_id")),
            })

    def _on(self, plmn):
        return sorted((c for c in self.all if plmn in c["plmns"] and c["name"]), key=lambda c: c["id"])

    def operator(self, plmn, apns):
        """The network's own carrier: the one preferring one of its APNs,
        else the earliest top-level carrier on it (ids follow registration:
        the network operator comes before its MVNOs)."""
        cs = [c for c in self._on(plmn) if not c["parent"]]
        hit = [c for c in cs if c["apns"] & apns]
        return (hit or cs or [{"name": None}])[0]["name"]

    def brand(self, plmn, a):
        """An MVNO/brand preset's carrier, by preferred APN or by GID1."""
        apn = (a.get("apn") or "").lower()
        gid = (a.get("mvno_match_data") or "").lower() if (a.get("mvno_type") or "").lower() == "gid" else None
        for c in self._on(plmn):
            if apn in c["apns"] or (gid and gid in c["gid1"]):
                return c["name"]
        return None


def types(a):
    ts = {t.strip().lower() for t in (a.get("type") or "").split(",")}
    return ts | {"default", "mms"} if "*" in ts else ts


def usages(a):
    us = [u for t, u in USAGE if t in types(a)]
    # oFono's default IMS context is APN "ims" already.
    return [u for u in us if not (u == "ims" and (a.get("apn") or "").lower() == "ims")]


def own_entry(a):
    """The operator's own APN, not an MVNO or brand preset. Presets are
    matched by mvno_type, or listed for the user to pick (user_visible
    true), or hidden data APNs (Sony hides some MVNO presets that way; the
    operator's own hidden entries are IMS/XCAP ones)."""
    if a.get("mvno_type") or a.get("user_visible") == "true":
        return False
    return not (a.get("user_visible") == "false" and "default" in types(a))


def preset_name(a):
    """The brand, without the "(host operator)" suffix Android names carry:
    oFono matches the SIM's SPN as a case-insensitive substring, so an au
    SIM (SPN "au") would pick "So-net(au)" over au's own APN."""
    if (a.get("mvno_type") or "").lower() == "spn" and a.get("mvno_match_data"):
        return a.get("mvno_match_data")
    name = a.get("carrier") or ""
    return re.sub(r"\s*[(（][^()（）]*[)）]\s*$", "", name) or name or "%s%s" % (a.get("mcc"), a.get("mnc"))


def common_name(names):
    prefix = os.path.commonprefix(names).rstrip(" -_(/&")
    return prefix if len(prefix) >= 2 else None


def apn_xml(a, usage):
    x = ['        <apn value="%s">' % escape(a.get("apn") or "", {'"': "&quot;"})]
    x.append('          <usage type="%s"/>' % usage)
    x.append('          <name>%s</name>' % NAME[usage])
    proto = PROTO.get((a.get("protocol") or "").upper())
    if proto:
        x.append('          <protocol type="%s"/>' % proto)
    if a.get("user"):
        x.append('          <username>%s</username>' % escape(a.get("user")))
    if a.get("password"):
        x.append('          <password>%s</password>' % escape(a.get("password")))
    if a.get("authtype") in AUTH:
        x.append('          <authentication method="%s"/>' % AUTH[a.get("authtype")])
    if usage == "mms" and a.get("mmsc"):
        x.append('          <mmsc>%s</mmsc>' % escape(a.get("mmsc")))
        if a.get("mmsproxy"):
            port = a.get("mmsport")
            x.append('          <mmsproxy>%s</mmsproxy>' % escape(a.get("mmsproxy") + (":" + port if port else "")))
    x.append('        </apn>')
    return x


class Provider:
    def __init__(self):
        self.plmns, self.apns, self.seen = [], [], set()

    def add(self, a, us):
        plmn = (a.get("mcc"), a.get("mnc"))
        if plmn not in self.plmns:
            self.plmns.append(plmn)
        for u in us:
            key = ((a.get("apn") or "").lower(), u)
            if key not in self.seen:
                self.seen.add(key)
                self.apns.append((a, u))


def convert(apns_path, carriers, countries):
    skipped = collections.Counter()
    own = collections.defaultdict(list)       # (mcc, mnc) -> [(apn, usages)]
    presets = []                              # [(country, apn, usages)]
    for a in ET.parse(apns_path).getroot().iter("apn"):
        if not a.get("mcc") or not a.get("mnc") or not a.get("apn"):
            skipped["no mcc/mnc/apn"] += 1
            continue
        if not countries.get(a.get("mcc")):
            skipped["unknown country"] += 1
            continue
        us = usages(a)
        if not us:
            skipped["no internet/mms/non-default ims"] += 1
            continue
        if own_entry(a):
            own[(a.get("mcc"), a.get("mnc"))].append((a, us))
        else:
            presets.append((countries[a.get("mcc")], a, us))

    # country -> (name, primary) -> Provider
    tree = collections.defaultdict(dict)
    for (mcc, mnc), entries in own.items():
        # The carrier-ID name ("NTT DOCOMO"); else, as apns-conf names APNs
        # rather than operators, the common part of the APN labels, else the
        # first label.
        name = (carriers.operator(mcc + mnc, {(a.get("apn") or "").lower() for a, us in entries if "internet" in us})
                or common_name([a.get("carrier") or "" for a, _ in entries])
                or entries[0][0].get("carrier") or mcc + mnc)
        p = tree[countries[mcc]].setdefault((name, True), Provider())
        for a, us in entries:
            p.add(a, us)
    # Per brand and network: the same brand has other APNs on other hosts.
    for country, a, us in presets:
        plmn = a.get("mcc") + a.get("mnc")
        key = (carriers.brand(plmn, a) or preset_name(a), False, plmn)
        tree[country].setdefault(key, Provider()).add(a, us)
    return tree, skipped


def write(tree, out, source):
    x = ['<?xml version="1.0" encoding="utf-8"?>',
         '<!-- Generated by apns2ofono.py from %s.' % escape(source),
         '     Source data, Apache License 2.0: apns-conf.xml, Copyright 2006 The Android',
         '     Open Source Project and Copyright (C) 2015 Sony Mobile Communications Inc.;',
         '     carrier names from carrier_list.textpb, The Android Open Source Project.',
         '     Converted to the mobile-broadband-provider-info format. -->',
         '<serviceproviders format="2.0">']
    for country in sorted(tree):
        x.append('  <country code="%s">' % country)
        # Operators first: libnma and NetworkManager take the first match.
        for (name, prim, *_), p in sorted(tree[country].items(), key=lambda kv: not kv[0][1]):
            x.append('    <provider%s>' % (' primary="true"' if prim else ""))
            x.append('      <name>%s</name>' % escape(name))
            x.append('      <gsm>')
            for mcc, mnc in p.plmns:
                x.append('        <network-id mcc="%s" mnc="%s"/>' % (mcc, mnc))
            for a, u in p.apns:
                x += apn_xml(a, u)
            x.append('      </gsm>')
            x.append('    </provider>')
        x.append('  </country>')
    x.append('</serviceproviders>')
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(x) + "\n")


def main():
    apns, carrier_list, countries_src, out = sys.argv[1:5]
    tree, skipped = convert(apns, Carriers(carrier_list), mcc_countries(countries_src))
    write(tree, out, apns.rsplit("/", 1)[-1])
    n = sum(len(p.apns) for c in tree.values() for p in c.values())
    print("wrote %s: %d countries, %d providers, %d apn entries; skipped %s" % (
        out, len(tree), sum(len(c) for c in tree.values()), n, dict(skipped)), file=sys.stderr)


def ofono_pick(path, mcc, mnc, spn):
    """What sailfish_provision.c would provision: {usage: (apn, provider, proto)}."""
    def match(s1, s2):
        if not s1 or not s2:
            return 0
        if s1 == s2:
            return 3
        a, b = s1.casefold(), s2.casefold()
        return 2 if a == b else 1 if a in b or b in a else 0
    cands = []
    for prov in ET.parse(path).getroot().iter("provider"):
        if not any(n.get("mcc") == mcc and n.get("mnc") == mnc for n in prov.iter("network-id")):
            continue
        for apn in prov.iter("apn"):
            score = 4 * match(prov.findtext("name"), spn) + match(apn.findtext("name"), spn)
            proto = apn.find("protocol")
            cands.append((score, prov.get("primary") == "true", apn.find("usage").get("type"),
                          apn.get("value"), prov.findtext("name"), proto.get("type") if proto is not None else "default"))
    out = {}
    for c in sorted(cands, key=lambda c: (-c[0], not c[1])):   # stable: file order on ties
        out.setdefault(c[2], c[3:])
    return out


def settings_list(path, mcc, mnc):
    """What GNOME Settings lists: the first provider's non-MMS APNs."""
    for prov in ET.parse(path).getroot().iter("provider"):
        if any(n.get("mcc") == mcc and n.get("mnc") == mnc for n in prov.iter("network-id")):
            return prov.findtext("name"), [a.get("value") for a in prov.iter("apn")
                                           if "mms" not in (a.get("value") + (a.findtext("name") or "")).lower()]
    return None, []


def check(path):
    cases = [("440", "10", "docomo", "spmode.ne.jp"),         # operator SIM
             ("440", "10", "IIJmio", "iijmio.jp"),            # MVNO on the same PLMN, by SPN
             ("440", "51", "au", "uad5gn.au-net.ne.jp"),      # hidden MVNO preset stays out
             ("460", "11", "中国电信", "ctnet")]
    for mcc, mnc, spn, want in cases:
        got = ofono_pick(path, mcc, mnc, spn)
        print(mcc, mnc, spn, got, "| Settings:", settings_list(path, mcc, mnc))
        assert got["internet"][0] == want, (mcc, mnc, got)
    assert "ims" not in ofono_pick(path, "460", "11", "中国电信"), "plain ims is oFono's default"
    assert settings_list(path, "460", "11")[1] == ["ctnet"]   # no MMS, no IMS, no duplicate
    names = {plmn: settings_list(path, *plmn)[0] for plmn in (("440", "10"), ("440", "51"), ("440", "20"), ("460", "11"))}
    assert names == {("440", "10"): "NTT DOCOMO", ("440", "51"): "au", ("440", "20"): "SoftBank",
                     ("460", "11"): "China Telecom"}, names
    brands = {p.findtext("name") for p in ET.parse(path).getroot().iter("provider")
              if any((n.get("mcc"), n.get("mnc")) == ("440", "10") for n in p.iter("network-id"))}
    assert {"nuromobile", "IIJmio", "mineo"} <= brands, brands
    for prov in ET.parse(path).getroot().iter("provider"):
        keys = [(a.get("value").lower(), a.find("usage").get("type")) for a in prov.iter("apn")]
        assert len(keys) == len(set(keys)), (prov.findtext("name"), keys)
    print("ok")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--check":
        check(sys.argv[2])
    elif len(sys.argv) == 5:
        main()
    else:
        sys.exit(__doc__)
