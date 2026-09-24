import { describe, expect, it } from 'vitest';
import { loadConfig } from '../src/config';
import { OddsApiClient } from '../src/sources/oddsApiClient';

describe('loadConfig — ODDS_API_MARKETS', () => {
  it('defaults to the three markets the odds endpoint and the parser support', () => {
    expect(loadConfig({}, []).oddsApi.markets).toEqual(['h2h', 'spreads', 'totals']);
    expect(loadConfig({ ODDS_API_MARKETS: 'H2H, totals,h2h' }, []).oddsApi.markets).toEqual(['h2h', 'totals']);
  });

  it('stops at startup on markets the odds endpoint rejects (instead of every league failing later)', () => {
    expect(() => loadConfig({ ODDS_API_MARKETS: 'h2h,spreads,alternate_spreads' }, [])).toThrow(/ODDS_API_MARKETS.*alternate_spreads/);
    expect(() => loadConfig({ ODDS_API_MARKETS: 'spread' }, [])).toThrow(/ODDS_API_MARKETS may only contain h2h, spreads, totals/);
    expect(() => loadConfig({ ODDS_API_MARKETS: 'player_points' }, [])).toThrow(/ODDS_API_MARKETS/);
  });
});

describe('loadConfig — ALLOWED_HOSTS', () => {
  it('is empty by default and parses host names and *. wildcards', () => {
    expect(loadConfig({}, []).server.allowedHosts).toEqual([]);
    expect(loadConfig({ ALLOWED_HOSTS: 'Odds.Example.com, *.ts.net ,vps.' }, []).server.allowedHosts).toEqual([
      'odds.example.com',
      '*.ts.net',
      'vps',
    ]);
  });

  it('rejects URLs, ports and paths with a clear message', () => {
    for (const bad of ['https://odds.example.com', 'odds.example.com:443', 'odds.example.com/x', 'a..b', '*']) {
      expect(() => loadConfig({ ALLOWED_HOSTS: bad }, []), bad).toThrow(/ALLOWED_HOSTS entries must be host names/);
    }
  });
});

describe('loadConfig — frozen-market limits', () => {
  it('defaults to 90 s live / 300 s pre-match and validates overrides', () => {
    const cfg = loadConfig({}, []);
    expect(cfg.oddsApi.maxMarketLagLiveSec).toBe(90);
    expect(cfg.oddsApi.maxMarketLagPrematchSec).toBe(300);
    expect(loadConfig({ ODDS_API_MAX_MARKET_LAG_LIVE_SEC: '60' }, []).oddsApi.maxMarketLagLiveSec).toBe(60);
    expect(() => loadConfig({ ODDS_API_MAX_MARKET_LAG_LIVE_SEC: '5' }, [])).toThrow(/>= 20/);
  });
});

describe('no-key guidance', () => {
  it('tells Docker users to use `docker compose up -d` and DEMO_MODE (npm scripts do not exist in the image)', () => {
    const h = new OddsApiClient(loadConfig({}, []).oddsApi).health();
    expect(h.status).toBe('disabled');
    expect(h.detail).toContain('docker compose up -d');
    expect(h.detail).toContain('DEMO_MODE=true');
    expect(h.detail).toContain('npm run demo');
  });
});
