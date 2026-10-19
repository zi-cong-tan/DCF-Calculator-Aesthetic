"""Pull the last few fiscal years from SEC filings (XBRL company facts) into data/history.json.

Deterministic: every number is a reported 10-K figure, no estimates.
Run: SEC_CONTACT=you@example.com python3 tools/sec_history.py  (the SEC asks every request to name a contact)
The SEC blocks browser requests (no CORS), so the site reads the saved file instead.
"""
import json, os, urllib.request, datetime, pathlib

COMPANIES = {'MU': 723125, 'AAPL': 320193, 'AMZN': 1018724, 'TSLA': 1318605}
YEARS = 3
UA = 'dcf-calculator ' + os.environ['SEC_CONTACT']

# First tag a company reports wins. Flows are full-year amounts, stocks are year-end balances.
FLOWS = {
    'revenue': ['RevenueFromContractWithCustomerExcludingAssessedTax', 'Revenues', 'SalesRevenueNet'],
    'ebit': ['OperatingIncomeLoss'],
    'da': ['DepreciationDepletionAndAmortization', 'DepreciationAmortizationAndAccretionNet', 'DepreciationAndAmortization',
           'Depreciation'],  # last resort (Tesla tags only this): leaves out amortization, so D&A reads slightly low
    'capex': ['PaymentsToAcquirePropertyPlantAndEquipment', 'PaymentsToAcquireProductiveAssets'],
}
STOCKS = {
    'ca': ['AssetsCurrent'], 'cl': ['LiabilitiesCurrent'],
    'cash': ['CashAndCashEquivalentsAtCarryingValue'],
    'sti': ['ShortTermInvestments', 'AvailableForSaleSecuritiesDebtSecuritiesCurrent', 'MarketableSecuritiesCurrent'],
    'debt': ['LongTermDebtCurrent', 'DebtCurrent', 'LongTermDebtAndCapitalLeaseObligationsCurrent'],
    'cp': ['CommercialPaper'],
}


def facts(cik):
    req = urllib.request.Request(f'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json', headers={'User-Agent': UA})
    return json.load(urllib.request.urlopen(req, timeout=30))['facts'].get('us-gaap', {})


def annual(g, tags, flow):
    """{fiscal-year-end date: value} from 10-K filings, using the first tag that covers the latest year."""
    best = {}
    for tag in tags:
        out = {}
        for x in g.get(tag, {}).get('units', {}).get('USD', []):
            if x.get('form') != '10-K':
                continue
            if flow:
                days = (datetime.date.fromisoformat(x['end']) - datetime.date.fromisoformat(x['start'])).days
                if not 350 <= days <= 380:
                    continue
            out[x['end']] = x['val']  # later filings restate earlier ones; keep the latest
        if out and (not best or max(out) > max(best)):
            best = out
    return best


def company(cik):
    g = facts(cik)
    f = {k: annual(g, t, True) for k, t in FLOWS.items()}
    s = {k: annual(g, t, False) for k, t in STOCKS.items()}
    ends = sorted(f['revenue'])[-(YEARS + 1):]

    def nwc(d):  # non-cash working capital: operating current assets less operating current liabilities
        v = lambda k: s[k].get(d, 0)
        return (v('ca') - v('cash') - v('sti')) - (v('cl') - v('debt') - v('cp'))

    rows = []
    for prev, d in zip(ends, ends[1:]):
        rev, m = f['revenue'][d], 1e6
        dnwc = nwc(d) - nwc(prev) if s['ca'].get(prev) and s['ca'].get(d) else None
        rows.append({'end': d, 'revenue': rev / m, 'ebit': f['ebit'].get(d, 0) / m, 'da': f['da'].get(d, 0) / m,
                     'capex': f['capex'].get(d, 0) / m, 'dnwc': None if dnwc is None else dnwc / m})
    return rows


if __name__ == '__main__':
    out = {'source': 'SEC XBRL company facts, 10-K filings', 'pulled': datetime.date.today().isoformat(),
           'companies': {t: company(c) for t, c in COMPANIES.items()}}
    p = pathlib.Path(__file__).resolve().parent.parent / 'data' / 'history.json'
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps(out, indent=1))
    for t, rows in out['companies'].items():
        for r in rows:
            print(t, r['end'], {k: round(v) if v is not None else None for k, v in r.items() if k != 'end'})
