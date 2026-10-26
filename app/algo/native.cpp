// The backtest's hot loops in C++. Built by deploy/update.sh:
//     g++ -O2 -std=c++17 -shared -fPIC -o app/algo/_native.so app/algo/native.cpp
// and checked against the Python engine before it is kept (python -m app.algo.native_check).
// No .so (a dev box without g++) = engine.py runs everything in Python.
//
// Both entry points are line-for-line ports of engine.py and give BIT-IDENTICAL
// answers: same double arithmetic in the same order (never build with -ffast-math),
// CPython's MT19937 for the Monte Carlo, Python's round(x, 6) for snap().
//
//   mc_paths  -- engine._mc_paths (the Monte Carlo's resampled paths)
//   run_loop  -- engine.run's candle loop for the strategies engine._native_ok()
//                accepts (shares, intraday). Entries and exits are booked by
//                calling back into engine.run's own open_ / close, so sizing,
//                margin, charges and trade records stay in Python, unchanged.
#include <charconv>
#include <cmath>
#include <cstdint>
#include <queue>
#include <vector>

#ifdef _WIN32
#define API extern "C" __declspec(dllexport)
#else
#define API extern "C"
#endif

typedef int64_t i64;

// ---- Monte Carlo ------------------------------------------------------------

static const int MT_N = 624, MT_M = 397;

struct MT {                                   // CPython's Modules/_randommodule.c
    uint32_t mt[MT_N];
    int mti = MT_N + 1;

    void init_genrand(uint32_t s) {
        mt[0] = s;
        for (mti = 1; mti < MT_N; ++mti)
            mt[mti] = 1812433253u * (mt[mti - 1] ^ (mt[mti - 1] >> 30)) + (uint32_t)mti;
    }
    void init_by_array(const uint32_t* key, int len) {      // random.Random(int) with a one-word key
        init_genrand(19650218u);
        int i = 1, j = 0;
        for (int k = MT_N > len ? MT_N : len; k; --k) {
            mt[i] = (mt[i] ^ ((mt[i - 1] ^ (mt[i - 1] >> 30)) * 1664525u)) + key[j] + (uint32_t)j;
            ++i; ++j;
            if (i >= MT_N) { mt[0] = mt[MT_N - 1]; i = 1; }
            if (j >= len) j = 0;
        }
        for (int k = MT_N - 1; k; --k) {
            mt[i] = (mt[i] ^ ((mt[i - 1] ^ (mt[i - 1] >> 30)) * 1566083941u)) - (uint32_t)i;
            ++i;
            if (i >= MT_N) { mt[0] = mt[MT_N - 1]; i = 1; }
        }
        mt[0] = 0x80000000u;
    }
    uint32_t next() {
        static const uint32_t mag01[2] = {0x0u, 0x9908b0dfu};
        uint32_t y;
        if (mti >= MT_N) {
            int kk = 0;
            for (; kk < MT_N - MT_M; ++kk) {
                y = (mt[kk] & 0x80000000u) | (mt[kk + 1] & 0x7fffffffu);
                mt[kk] = mt[kk + MT_M] ^ (y >> 1) ^ mag01[y & 1u];
            }
            for (; kk < MT_N - 1; ++kk) {
                y = (mt[kk] & 0x80000000u) | (mt[kk + 1] & 0x7fffffffu);
                mt[kk] = mt[kk + (MT_M - MT_N)] ^ (y >> 1) ^ mag01[y & 1u];
            }
            y = (mt[MT_N - 1] & 0x80000000u) | (mt[0] & 0x7fffffffu);
            mt[MT_N - 1] = mt[MT_M - 1] ^ (y >> 1) ^ mag01[y & 1u];
            mti = 0;
        }
        y = mt[mti++];
        y ^= (y >> 11);
        y ^= (y << 7) & 0x9d2c5680u;
        y ^= (y << 15) & 0xefc60000u;
        y ^= (y >> 18);
        return y;
    }
    uint32_t below(uint32_t n) {                            // randrange(n), 0 < n < 2**32
        int k = 32 - __builtin_clz(n);                      // n.bit_length()
        uint32_t r = next() >> (32 - k);                    // getrandbits(k)
        while (r >= n) r = next() >> (32 - k);
        return r;
    }
};

API void mc_paths(const double* nets, i64 n, i64 runs, uint32_t seed, double capital,
                  double* finals, double* dds, i64* ruined) {
    MT rnd;
    rnd.init_by_array(&seed, 1);
    i64 broke_n = 0;
    for (i64 run = 0; run < runs; ++run) {
        double eq = capital, peak = capital, dd = 0.0;
        bool broke = false;
        for (i64 j = 0; j < n; ++j) {
            eq += nets[rnd.below((uint32_t)n)];
            if (eq > peak) peak = eq;
            double d = peak > 0 ? (peak - eq) / peak * 100 : 100.0;
            if (d > dd) dd = d;
            broke = broke || eq <= capital / 2;
        }
        finals[run] = eq - capital;
        dds[run] = dd;
        broke_n += broke;
    }
    *ruined = broke_n;
}

// ---- candle loop ------------------------------------------------------------

static const i64 IST = 19800;
static const double NONE = NAN;               // Python None, for the optional prices
static bool has(double x) { return !std::isnan(x); }

static double py_round6(double x) {           // Python's round(x, 6): correctly rounded, then parsed back
    char buf[64];
    auto r = std::to_chars(buf, buf + sizeof buf, x, std::chars_format::fixed, 6);
    double out = x;
    std::from_chars(buf, r.ptr, out);
    return out;
}

static double snap0(double px, double tick) {  // engine.snap(px, tick) with side 0
    if (!has(tick) || tick == 0 || !std::isfinite(px)) return px;
    double k = std::floor(px / tick + 0.5);
    return py_round6((k > 1 ? k : 1) * tick);
}

static double solve_id(double target, double a, double b) {   // engine._solve with f(u) = u
    double fa = a - target;
    for (int it = 0; it < 40; ++it) {
        double m = (a + b) / 2, fm = m - target;
        if ((fm <= 0) == (fa <= 0)) { a = m; fa = fm; } else b = m;
    }
    return (a + b) / 2;
}

struct Sym {                                  // one symbol's candles and signals (ctypes mirror in engine.py)
    const i64* t; const double *o, *h, *l, *c;
    const i64* tc;                            // close times, or null: time candles close at t + tf_s
    const uint8_t *el, *es, *xl, *xs;         // entry_long / entry_short / exit_long / exit_short
    i64 n, tf_s;
    double tick;                              // NaN = no price grid
};

struct Cfg {                                  // the spec values the loop reads (ctypes mirror in engine.py)
    i64 start, end, sq, max_trades_per_day, cooldown, max_bars, max_positions, trade_from,
        days_mask, entry_kind, valid_bars;    // entry_kind: 0 market, 1 pullback, 2 breakout
    double daily_max_loss, daily_max_profit;  // 0 = off
};

enum Reason { EOD = 1, SQUARE_OFF, SIGNAL, TIME_STOP, DAILY_MAX_LOSS, DAILY_TARGET, SL, TRAIL, BREAKEVEN,
              TARGET, END };

// open: 0 = opened (out = sign, ref_entry, sl, tg, trd, tra, be, u0, mid_bar, act, entry, qty),
//       1 = skipped (engine.run counted why), -1 = Python raised.
typedef int (*OpenCb)(i64 k, int buy, i64 i, i64 sig_i, double fill, double* out);
typedef int (*CloseCb)(i64 k, double u, i64 t, int reason, i64 i, int slipped, double best, double worst,
                       double* net);
typedef int (*TrigCb)(i64 k, int buy, i64 i, double* level);   // entry_trigger(); level NaN = None

struct Pos {
    bool open = false, buy = false, be_on = false, mid_bar = false;
    int sign = 1, exit_pending = 0;
    double ref_entry = 0, sl = NONE, tg = NONE, trd = NONE, tra = 0, be = NONE, trail = NONE;
    double best = 0, worst = 0, u0 = 0, act = 0, entry = 0, qty = 0;
    i64 i = 0, day = 0;
};
struct Pend { bool on = false, buy = false; i64 day = 0, i = 0, expires = 0; double level = NONE; };
struct St { Pos pos; Pend pend; i64 pd_day = -1, pd_n = 0, cool = 0; bool has_u = false; double u_c = 0; };

static i64 close_time(const Sym& d, i64 i) {               // indicators.close_time
    if (d.tc) return d.tc[i];
    i64 t = d.t[i], a = t + d.tf_s, b = t - (t + IST) % 86400 + 55800;
    return a < b ? a : b;
}

API int run_loop(const Sym* S, i64 nsym, const Cfg* cfg, OpenCb op, CloseCb cl, TrigCb tr, i64* skipped) {
    // skipped: [daily_halt, entry_expired, max_positions] — the counts engine.run keeps itself.
    // Returns 0 done, -1 a callback raised, 2 not handled (nothing was run: the Python loop takes it).
    for (i64 k = 0; k < nsym; ++k)            // engine._events sorts a symbol whose candles aren't ascending
        for (i64 i = 1; i < S[k].n; ++i)
            if (S[k].t[i] <= S[k].t[i - 1]) return 2;
    std::vector<St> st(nsym);
    i64 day_key = INT64_MIN, halted = INT64_MIN, open_n = 0;
    double day_realized = 0.0;
    double out[12];

    auto close = [&](i64 k, double u, i64 t, int reason, i64 i, int slipped) -> bool {
        Pos& p = st[k].pos;
        double net = 0;
        if (cl(k, u, t, reason, i, slipped, p.best, p.worst, &net) < 0) return false;
        if ((t + IST) / 86400 == day_key) day_realized += net;   // booked at an earlier day's candle: not today's
        p.open = false;
        --open_n;
        st[k].cool = i + cfg->cooldown;
        return true;
    };
    auto open = [&](i64 k, bool buy, i64 i, i64 sig_i, double fill) -> int {
        int r = op(k, buy, i, sig_i, fill, out);
        if (r != 0) return r;
        Pos& p = st[k].pos;
        p = Pos();
        p.open = true; p.buy = buy;
        p.sign = (int)out[0]; p.ref_entry = out[1]; p.sl = out[2]; p.tg = out[3]; p.trd = out[4]; p.tra = out[5];
        p.be = out[6]; p.u0 = out[7]; p.mid_bar = out[8] != 0; p.act = out[9]; p.entry = out[10]; p.qty = out[11];
        p.best = p.worst = p.ref_entry;
        p.i = i;
        p.day = (S[k].t[i] + IST) / 86400;
        ++open_n;
        St& s = st[k];
        if (s.pd_day == p.day) ++s.pd_n; else { s.pd_day = p.day; s.pd_n = 1; }
        return 0;
    };
    // engine.run's check_intrabar for a one-leg share position: stop first, then target.
    auto intrabar = [&](i64 k, i64 i) -> bool {
        const Sym& d = S[k];
        Pos& p = st[k].pos;
        double o = d.o[i], h = d.h[i], l = d.l[i];
        i64 t = d.t[i];
        int sgn = p.sign;
        bool later = i > p.i, fav_ok = later || !p.mid_bar;
        double u0 = fav_ok ? o : p.u0;
        double stop = NONE;
        for (double x : {p.sl, p.trail, p.be_on ? p.ref_entry : NONE})
            if (has(x) && (!has(stop) || (sgn > 0 ? x > stop : x < stop))) stop = x;
        if (has(stop)) {
            int why = (has(p.trail) && stop == p.trail) ? TRAIL
                    : (p.be_on && stop == p.ref_entry && stop != p.sl) ? BREAKEVEN : SL;
            double u_adv = (sgn * l < sgn * h) ? l : h;            // min((h, l), key=sgn*u): h on a tie
            if (later && sgn * (o - stop) <= 0) return close(k, o, t, why, i, 1);
            if (sgn * (u_adv - stop) <= 0) return close(k, solve_id(stop, u0, u_adv), t, why, i, 1);
        }
        if (has(p.tg) && fav_ok) {
            double u_fav = (sgn * l > sgn * h) ? l : h;            // max((h, l), key=sgn*u): h on a tie
            if (later && sgn * (o - p.tg) >= 0) return close(k, o, t, TARGET, i, 0);
            if (sgn * (u_fav - p.tg) >= 0) return close(k, solve_id(p.tg, o, u_fav), t, TARGET, i, 0);
        }
        return true;
    };

    // engine._events: every candle of every symbol by (t, symbol index); each symbol's own are ascending.
    typedef std::pair<i64, i64> Ev;                                // (t, k); i = cursor[k]
    std::priority_queue<Ev, std::vector<Ev>, std::greater<Ev>> heap;
    std::vector<i64> cur(nsym, 0);
    for (i64 k = 0; k < nsym; ++k) if (S[k].n) heap.push({S[k].t[0], k});

    while (!heap.empty()) {
        i64 k = heap.top().second;
        heap.pop();
        i64 i = cur[k]++;
        if (cur[k] < S[k].n) heap.push({S[k].t[cur[k]], k});
        const Sym& d = S[k];
        St& s = st[k];
        Pos& p = s.pos;
        i64 t = d.t[i], day = (t + IST) / 86400, mod = ((t + IST) % 86400) / 60;
        if (day != day_key) { day_key = day; day_realized = 0.0; }
        if (p.open && day != p.day && !close(k, d.c[i - 1], close_time(d, i - 1), EOD, i - 1, 1)) return -1;
        if (p.open && (p.exit_pending || mod >= cfg->sq)
            && !close(k, d.o[i], t, p.exit_pending ? p.exit_pending : SQUARE_OFF, i, 1)) return -1;
        if (s.pend.on) {
            Pend& q = s.pend;
            double fill = NONE, lo = d.l[i], hi = d.h[i], o = d.o[i];
            if (!has(q.level)) fill = o;
            else if (q.buy == (cfg->entry_kind == 1)) { if (lo <= q.level) fill = q.level < o ? q.level : o; }
            else if (hi >= q.level) fill = q.level > o ? q.level : o;
            bool alive = q.day == day && mod < cfg->sq && halted != day && !p.open && i < q.expires;
            if (!(!has(fill) && alive)) {
                q.on = false;
                if (halted == day) ++skipped[0];
                else if (!has(fill) || !alive) { if (has(q.level)) ++skipped[1]; }
                else if (open_n >= cfg->max_positions) ++skipped[2];
                else if (open(k, q.buy, i, q.i, fill) < 0) return -1;
            }
        }
        if (p.open && !intrabar(k, i)) return -1;
        if (p.open) {
            int sgn = p.sign;
            double h = d.h[i], l = d.l[i], c = d.c[i];
            double fa = h, fb = l;                                  // fav: (u0, c) on a mid-bar fill's own candle
            if (i == p.i && p.mid_bar) { fa = p.u0; fb = c; }
            double best_now = (sgn * fb > sgn * fa) ? fb : fa;      // max(fav, key=sgn*u): the first on a tie
            double worst_now = (sgn * l < sgn * h) ? l : h;         // min((h, l), key=sgn*u)
            if (sgn > 0) { if (best_now > p.best) p.best = best_now; if (worst_now < p.worst) p.worst = worst_now; }
            else { if (best_now < p.best) p.best = best_now; if (worst_now > p.worst) p.worst = worst_now; }
            double gain = sgn * (p.best - p.ref_entry);
            if (has(p.trd) && p.trd != 0 && gain >= p.tra) {
                double nt = snap0(p.best - sgn * p.trd, d.tick);
                if (!has(p.trail)) p.trail = nt;
                else if (sgn > 0 ? nt > p.trail : nt < p.trail) p.trail = nt;
            }
            if (has(p.be) && p.be != 0 && gain >= p.be) p.be_on = true;
            if ((p.buy ? d.xl : d.xs)[i]) p.exit_pending = SIGNAL;
            else if (cfg->max_bars && i - p.i + 1 >= cfg->max_bars) p.exit_pending = TIME_STOP;
        }
        s.has_u = true;
        s.u_c = d.c[i];
        if ((cfg->daily_max_loss != 0 || cfg->daily_max_profit != 0) && halted != day) {
            double day_pnl = day_realized;
            for (i64 j = 0; j < nsym; ++j) {          // ponytail: symbol order, Python sums open_syms (a set) — equal unless a sum lands exactly on the limit
                const St& s2 = st[j];
                if (s2.pos.open && s2.has_u && s2.pos.day == day)
                    day_pnl += s2.pos.act * (s2.u_c - s2.pos.entry) * s2.pos.qty;
            }
            int hit = (cfg->daily_max_loss != 0 && day_pnl <= -cfg->daily_max_loss) ? DAILY_MAX_LOSS
                    : (cfg->daily_max_profit != 0 && day_pnl >= cfg->daily_max_profit) ? DAILY_TARGET : 0;
            if (hit) {
                halted = day;
                for (St& s2 : st) { if (s2.pos.open) s2.pos.exit_pending = hit; s2.pend.on = false; }
            }
        }
        if (!p.open && !s.pend.on && i + 1 < d.n && i >= s.cool && halted != day && t >= cfg->trade_from) {
            i64 close_mod = ((close_time(d, i) + IST) % 86400) / 60;
            i64 wd = ((t + IST) / 86400 + 3) % 7 + 1;              // engine.day_allowed: 1 = Monday
            if (cfg->start <= close_mod && close_mod <= cfg->end && (s.pd_day == day ? s.pd_n : 0) < cfg->max_trades_per_day
                && (!cfg->days_mask || (cfg->days_mask >> wd) & 1)) {
                int side = d.el[i] ? 1 : d.es[i] ? 0 : -1;
                if (side >= 0) {
                    double level = NONE;
                    if (cfg->entry_kind != 0 && tr(k, side, i, &level) < 0) return -1;
                    s.pend.on = true; s.pend.buy = side == 1; s.pend.day = day; s.pend.i = i; s.pend.level = level;
                    s.pend.expires = i + 1 + cfg->valid_bars;
                }
            }
        }
    }
    for (i64 k = 0; k < nsym; ++k)
        if (st[k].pos.open && !close(k, S[k].c[S[k].n - 1], close_time(S[k], S[k].n - 1), END, S[k].n - 1, 1))
            return -1;
    return 0;
}
