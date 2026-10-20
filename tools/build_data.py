"""Build the searchable company data: one file per S&P 500 company, from SEC filings plus the last closing price.

Every figure is reported (10-K / 10-Q, the US Treasury, the market close), nothing is estimated.
Run: SEC_CONTACT=you@example.com python3 tools/build_data.py [TICKER ...]
  - The SEC asks every request to name a contact; it is sent only to the SEC and never saved.
  - With tickers, only those are rebuilt (the index still lists every company).
Writes data/index.json (ticker, name, sector, sub-industry, market value in $bn, for search and comps) and data/co/<TICKER>.json. GitHub Actions runs it nightly.
Prices and betas come from Alpaca when ALPACA_KEY and ALPACA_SECRET are set (free market data plan), else from Yahoo.
Browsers cannot call the SEC, Alpaca or Yahoo directly (no CORS, and Alpaca's key must stay secret), so the site reads these files.
"""
import csv, datetime, io, json, os, pathlib, sys, time, urllib.parse, urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent / 'data'
SEC_UA = 'dcf-calculator ' + os.environ['SEC_CONTACT']
WEB_UA = 'Mozilla/5.0'
SP500 = 'https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv'
YEARS = 3
ALPACA = {'APCA-API-KEY-ID': os.environ.get('ALPACA_KEY', ''), 'APCA-API-SECRET-KEY': os.environ.get('ALPACA_SECRET', '')}

# First tag a company reports wins. Flows are full-year amounts, stocks are balances at a date.
FLOWS = {
    'revenue': ['RevenueFromContractWithCustomerExcludingAssessedTax', 'Revenues', 'SalesRevenueNet',
                'RevenueFromContractWithCustomerIncludingAssessedTax'],
    'ebit': ['OperatingIncomeLoss'],
    'da': ['DepreciationDepletionAndAmortization', 'DepreciationAmortizationAndAccretionNet', 'DepreciationAndAmortization',
           'Depreciation'],  # last resort (Tesla tags only this): leaves out amortization, so D&A reads slightly low
    'capex': ['PaymentsToAcquirePropertyPlantAndEquipment', 'PaymentsToAcquireProductiveAssets',
              'PaymentsForProceedsFromProductiveAssets', 'PaymentsToAcquireOtherPropertyPlantAndEquipment',
              'PaymentsToAcquireOtherProductiveAssets', 'PaymentsForConstructionInProcess',  # utilities and telecoms
              'PaymentsToAcquireOilAndGasPropertyAndEquipment', 'PaymentsToDevelopRealEstateAssets'],
    # some (NextEra, DTE, CMS, ConocoPhillips) tag capex only under their own labels, which company facts leave out: stays empty
    'amort': ['AmortizationOfIntangibleAssets'],
    'ni': ['NetIncomeLoss', 'ProfitLoss'],  # for P/E in comps; the first is the parent's share  # for the EBITA margin; many companies don't tag it, then it stays empty
    'tax': ['IncomeTaxExpenseBenefit'],
    'interest': ['InterestExpense', 'InterestExpenseNonoperating', 'InterestExpenseDebt', 'InterestAndDebtExpense',
                 'InterestExpenseLongTermDebt',
                 'InterestPaidNet'],  # last resort: interest paid in cash, close to the expense
    'pretax': ['IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest',
               'IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments',
               'IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic'],  # last resort: US-only companies report just this
}
STOCKS = {
    'ca': ['AssetsCurrent'], 'cl': ['LiabilitiesCurrent'],
    'cash': ['CashAndCashEquivalentsAtCarryingValue', 'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents'],
    'sti': ['ShortTermInvestments', 'AvailableForSaleSecuritiesDebtSecuritiesCurrent', 'MarketableSecuritiesCurrent'],
    'debtc': ['LongTermDebtCurrent', 'DebtCurrent', 'LongTermDebtAndCapitalLeaseObligationsCurrent'],
    'cp': ['CommercialPaper', 'ShortTermBorrowings'],
    'debtn': ['LongTermDebtNoncurrent', 'LongTermDebtAndCapitalLeaseObligations'],
    'equity': ['StockholdersEquity'],
}


def get(url, ua, tries=3, headers={}):
    for k in range(tries):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': ua, **headers}), timeout=30).read()
        except Exception:
            if k == tries - 1:
                raise
            time.sleep(2 * (k + 1))


def series(g, tags, flow, forms=('10-K',)):
    """{period end: value} from the first tag that covers the latest period. Later filings restate earlier ones."""
    best = {}
    for tag in tags:
        out = {}
        for x in g.get(tag, {}).get('units', {}).get('USD', []):
            if x.get('form') not in forms:
                continue
            if flow:
                if not x.get('start'):
                    continue
                days = (datetime.date.fromisoformat(x['end']) - datetime.date.fromisoformat(x['start'])).days
                if not 350 <= days <= 380:
                    continue
            out[x['end']] = x['val']
        if out and (not best or max(out) > max(best)):
            best = out
    return best


def shares_out(facts):
    """Latest shares outstanding from the filing cover, else diluted weighted shares."""
    for ns, tag in (('dei', 'EntityCommonStockSharesOutstanding'), ('us-gaap', 'WeightedAverageNumberOfDilutedSharesOutstanding')):
        xs = facts.get(ns, {}).get(tag, {}).get('units', {}).get('shares', [])
        if xs:
            end = max(x['end'] for x in xs)
            last = max((x for x in xs if x['end'] == end), key=lambda x: x['filed'])['accn']
            return sum(x['val'] for x in xs if x['end'] == end and x['accn'] == last)  # share classes add up
    return None


def regress(closes, spx):
    """Raw beta from monthly closes, {month: close} for the stock and the index; None under three years."""
    common = [t for t in sorted(closes) if t in spx and closes[t] and spx[t]]
    if len(common) < 37:
        return None
    a = [closes[b] / closes[a] - 1 for a, b in zip(common, common[1:])]
    m = [spx[b] / spx[a] - 1 for a, b in zip(common, common[1:])]
    ma, mm = sum(a) / len(a), sum(m) / len(m)
    return sum((x - ma) * (y - mm) for x, y in zip(a, m)) / sum((y - mm) ** 2 for y in m)


def alpaca(symbols, timeframe, start):
    """Split- and dividend-adjusted closes, {symbol: {bar date: close}}, 100 symbols a request.
    The free plan serves full-market (SIP) bars only once they are 15 minutes old."""
    end = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=16)).strftime('%Y-%m-%dT%H:%M:%SZ')
    out = {}
    for i in range(0, len(symbols), 100):
        q = {'symbols': ','.join(symbols[i:i + 100]), 'timeframe': timeframe, 'start': start, 'end': end,
             'adjustment': 'all', 'feed': 'sip', 'limit': 10000}
        while True:
            j = json.loads(get('https://data.alpaca.markets/v2/stocks/bars?' + urllib.parse.urlencode(q), WEB_UA, headers=ALPACA))
            for sym, bars in (j.get('bars') or {}).items():
                out.setdefault(sym, {}).update((b['t'][:10], b['c']) for b in bars)
            if not j.get('next_page_token'):
                break
            q['page_token'] = j['next_page_token']
    return out


def market(ticker, spx, alp):
    """Last close, its date, and the raw 5-year monthly beta against the S&P 500 (SPY on Alpaca)."""
    if alp and ticker in alp[0] and ticker in alp[1]:
        day = max(alp[1][ticker])
        return alp[1][ticker][day], day, regress(alp[0][ticker], alp[0].get('SPY', {}))
    j = json.loads(get(f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker.replace(".", "-")}?range=5y&interval=1mo', WEB_UA))
    r = j['chart']['result'][0]
    px, when = r['meta']['regularMarketPrice'], datetime.datetime.fromtimestamp(r['meta']['regularMarketTime'], datetime.UTC).date().isoformat()
    return px, when, regress(dict(zip(r['timestamp'], r['indicators']['adjclose'][0]['adjclose'])), spx)


def company(cik, ticker, spx, alp, financial):
    facts = json.loads(get(f'https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik):010d}.json', SEC_UA))['facts']
    g = facts.get('us-gaap', {})
    f = {k: series(g, t, True) for k, t in FLOWS.items()}
    # capex: the largest of its labels each year, since some companies also tag a smaller part ("other" equipment)
    f['capex'] = {}
    for t in FLOWS['capex']:
        for d, v in series(g, [t], True).items():
            f['capex'][d] = max(v, f['capex'].get(d, v))
    s = {k: series(g, t, False) for k, t in STOCKS.items()}
    q = {k: series(g, t, False, ('10-K', '10-Q')) for k, t in STOCKS.items()}  # latest balance sheet, any filing
    ends = sorted(f['revenue'])[-(YEARS + 1):]
    m = 1e6

    def ebit(d):
        # No operating line (common in pharma and energy): pre-tax profit plus interest expense.
        # Not for financials, where interest is the cost of the business itself.
        if f['ebit'].get(d) is not None:
            return f['ebit'][d], False
        if not financial and f['pretax'].get(d) is not None:
            return f['pretax'][d] + f['interest'].get(d, 0), True
        return None, False

    ends = [d for d in ends if ebit(d)[0] is not None] if len(ends) > 1 else ends  # drop years with no profit figure
    if len(ends) < 2:
        return None

    def nwc(d):  # non-cash working capital: operating current assets less operating current liabilities
        v = lambda k: s[k].get(d, 0)
        return (v('ca') - v('cash') - v('sti')) - (v('cl') - v('debtc') - v('cp'))

    opt = lambda x, d: None if x.get(d) is None else x[d] / m  # None when not reported, not 0
    hist = []
    for prev, d in zip(ends, ends[1:]):
        dn = nwc(d) - nwc(prev) if s['ca'].get(prev) and s['ca'].get(d) else None
        e, proxy = ebit(d)
        hist.append({'end': d, 'revenue': f['revenue'][d] / m, 'ebit': e / m, 'ebitProxy': proxy, 'da': f['da'].get(d, 0) / m,
                     'capex': opt(f['capex'], d), 'dnwc': None if dn is None else dn / m,
                     'interest': opt(f['interest'], d), 'amort': opt(f['amort'], d), 'ni': opt(f['ni'], d)})
    rates = [f['tax'][d] / f['pretax'][d] for d in ends[1:] if f['tax'].get(d) is not None and f['pretax'].get(d, 0) > 0]

    bal = max(q['cash']) if q['cash'] else None
    at = lambda k: q[k].get(bal, 0) / m if bal else 0
    debt = at('debtc') + at('cp') + at('debtn')
    eq = q['equity'].get(bal) / m if bal and q['equity'].get(bal) is not None else None
    px, when, beta = market(ticker, spx, alp)
    return {'ticker': ticker, 'cik': int(cik), 'price': px, 'priceDate': when, 'shares': (shares_out(facts) or 0) / m,
            'balanceDate': bal, 'cash': at('cash') + at('sti'), 'debt': debt, 'equity': eq,
            'taxRate': sum(rates) / len(rates) if rates else None, 'betaRaw': beta, 'history': hist}


def ten_year():
    y = datetime.date.today().year
    rows = list(csv.reader(io.StringIO(get('https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/'
                                           f'{y}/all?type=daily_treasury_yield_curve&field_tdr_date_value={y}&page&_format=csv', WEB_UA).decode())))
    i = rows[0].index('10 Yr')
    return float(rows[1][i]) / 100, datetime.datetime.strptime(rows[1][0], '%m/%d/%Y').date().isoformat()


if __name__ == '__main__':
    members = list(csv.DictReader(io.StringIO(get(SP500, WEB_UA).decode())))
    only = {t.upper() for t in sys.argv[1:]}
    rf, rf_date = ten_year()
    j = json.loads(get('https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC?range=5y&interval=1mo', WEB_UA))['chart']['result'][0]
    spx = dict(zip(j['timestamp'], j['indicators']['adjclose'][0]['adjclose']))
    alp = None
    if all(ALPACA.values()):
        syms = [r['Symbol'] for r in members if not only or r['Symbol'] in only] + ['SPY']
        today = datetime.date.today()
        try:  # (monthly, daily); a company Alpaca lacks still falls back to Yahoo
            alp = (alpaca(syms, '1Month', today.replace(year=today.year - 5, day=1).isoformat()),
                   alpaca(syms, '1Day', (today - datetime.timedelta(days=10)).isoformat()))
        except Exception as e:
            print('Alpaca failed, prices from Yahoo:', str(e)[:120])
    (ROOT / 'co').mkdir(parents=True, exist_ok=True)
    index, failed = [], []
    for row in members:
        t = row['Symbol']
        if only and t not in only:
            if (ROOT / 'co' / f'{t}.json').exists():
                c = json.loads((ROOT / 'co' / f'{t}.json').read_text())
                index.append([t, row['Security'], row['GICS Sector'], row['GICS Sub-Industry'], round(c['price'] * c['shares'] / 1000, 1)])
            continue
        try:
            c = company(row['CIK'], t, spx, alp, row['GICS Sector'] == 'Financials')
        except Exception as e:  # one bad company never stops the run
            c, why = None, str(e)[:80]
        else:
            why = 'no operating or pre-tax profit, or under two years of revenue'
        if c and not c['history'][-1]['ebit']:
            c, why = None, 'no operating income reported'
        if c and c['price'] * c['shares'] < 1000:  # under $1bn: share count is off (e.g. share classes in other units)
            c, why = None, 'share count does not fit the price'
        if not c or not c['price']:
            failed.append(f'{t} ({why})')
            continue
        c.update(name=row['Security'], sector=row['GICS Sector'], rf=rf, rfDate=rf_date)
        (ROOT / 'co' / f'{t}.json').write_text(json.dumps(c, separators=(',', ':')))
        index.append([t, row['Security'], row['GICS Sector'], row['GICS Sub-Industry'], round(c['price'] * c['shares'] / 1000, 1)])
        time.sleep(0.15)  # the SEC allows 10 requests a second; stay well under
    (ROOT / 'index.json').write_text(json.dumps({'built': datetime.date.today().isoformat(), 'companies': index}, separators=(',', ':')))
    print(f'{len(index)} companies written, {len(failed)} skipped')
    for x in failed:
        print('  skipped', x)
