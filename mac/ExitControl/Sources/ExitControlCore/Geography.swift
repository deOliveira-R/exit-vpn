import Foundation

// Countries for display: flags, ISO codes from server names, and continents (menus).

/// Country flag from an ISO code ("US" → 🇺🇸).
public func flag(_ code: String?) -> String {
    guard var code = code?.uppercased(), code.count == 2, code != "XK" else { return "🌐" }  // XK: no emoji
    if code == "UK" { code = "GB" }   // Proton (and gluetun) use UK; the flag is ISO's GB
    return code.unicodeScalars.compactMap {
        UnicodeScalar(127397 + $0.value).map(String.init)
    }.joined()
}

/// ISO code from a Proton-style hostname (us-ca-01…, node-ch-03…), as the controller does.
/// The server's country from its name: "NG#1" and "US-NY#3" give the first code, a Secure Core
/// "CH-US#3" gives its exit (US). Hostnames can name another country (Proton hosts some virtual
/// locations elsewhere: NG#1 is node-es-…), so they are only the fallback.
public func countryCode(_ s: Server) -> String? {
    let parts = s.name.split(separator: "#").first?.split(separator: "-") ?? []
    let code = s.secureCore && parts.count > 1 ? parts[1] : parts.first
    if let c = code, c.count == 2, c.allSatisfy({ $0.isASCII && $0.isUppercase }) { return String(c) }
    return countryCode(hostname: s.hostname)
}

public func countryCode(hostname: String) -> String? {
    let parts = hostname.split(separator: ".").first?.split(separator: "-") ?? []
    let first = parts.first == "node" ? parts.dropFirst().first : parts.first
    guard let f = first, f.count == 2, f.allSatisfy(\.isLetter) else { return nil }
    return f.uppercased()
}

/// Continents for the "More Countries" menu. Proton's locations as of 2026; an unlisted code
/// lands in "Other". The Middle East is its own group (a long list in Asia otherwise).
public let continents = ["Africa", "Asia", "Europe", "Middle East", "North America", "Oceania", "South America"]

private let continentCodes: [String: String] = {
    let groups = [
        "Africa": "AO CD CI CM DZ EG ER ET GA GH GN KE KM LY MA MR MU MZ NG RW SD SN SO SS TD TG TN TZ UG ZA ZW",
        "Asia": "AF AM AZ BD BN BT CN GE HK ID IN JP KG KH KR KZ LA LK MM MN MO MY NP PH PK SG TH TJ TM TW UZ VN",
        "Europe": "AD AL AT BA BE BG BY CH CY CZ DE DK EE ES FI FR GB GR HR HU IE IS IT LI LT LU LV MC MD ME MK MT NL NO PL PT RO RS RU SE SI SK TR UA UK XK",
        "Middle East": "AE BH IL IQ IR JO KW LB OM PS QA SA SY YE",
        "North America": "BS CA CR CU DO GL GT HN HT JM MX NI PA PR SV US",
        "Oceania": "AU FJ NZ PG",
        "South America": "AR BO BR CL CO EC PE PY UY VE",
    ]
    var m: [String: String] = [:]
    for (c, codes) in groups { for code in codes.split(separator: " ") { m[String(code)] = c } }
    return m
}()

public func continent(_ code: String?) -> String {
    code.flatMap { continentCodes[$0.uppercased()] } ?? "Other"
}
