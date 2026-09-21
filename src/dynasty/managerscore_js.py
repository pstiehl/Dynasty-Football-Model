"""Client-side JavaScript for managerscore.html, kept in two halves.

``MANAGERSCORE_CORE_JS`` is pure: no DOM, no ``fetch``, no globals beyond
the functions it defines. That is deliberate — ``tests/test_managerscore.py``
evaluates it directly in ``node`` and asserts the scoring arithmetic against
synthetic leagues. Scoring logic lives here once; there is no second Python
implementation to drift out of sync with it.

``MANAGERSCORE_UI_JS`` is the impure half: Sleeper fetches, artifact loads,
rendering. It is exercised against a minimal stub DOM, also from
``tests/test_managerscore.py``.

Same token-substitution convention as ``reel.py`` / ``myteam.py`` — the JS
is a plain string so its braces stay readable, and build-time values are
spliced in with ``__TOKEN__`` replacement rather than f-strings.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Pure scoring core
# ---------------------------------------------------------------------------

MANAGERSCORE_CORE_JS = r"""
/* ============================================================
 * Manager Score — pure scoring core.
 * No DOM, no network. Everything below is a function of its
 * arguments, so tests/test_managerscore.py can run it in node.
 * ============================================================ */

/* Component weights. Drafts move the most value in a dynasty league and
 * every manager participates in them, so they carry the most weight.
 * Trades are higher-leverage per event but far rarer and self-selected.
 * Waiver adds are real skill but individually small. */
var MS_WEIGHTS = { draft: 0.50, trade: 0.35, waiver: 0.15 };

/* Empirical-Bayes shrinkage constants, in "transactions". A manager's
 * component mean is pulled toward zero by n/(n+k), so a single lucky pick
 * cannot top the board. k is roughly "how many events before I half-trust
 * the average", set to about one draft class, a typical season's trades,
 * and a handful of waiver hits respectively. */
var MS_SHRINK_K = { draft: 6, trade: 3, waiver: 5 };

/* Below this, a positional median means "most of this pool did not play"
 * rather than "this is what a typical alternative gave you". Measured on
 * four real seasons: QB medians of 0.00-0.90 occur in bye-heavy weeks and
 * in weeks the league had already stopped playing. */
var MS_MIN_BASELINE = 1.0;

/* A draft needs at least this many valued picks before we will fit a
 * slot-cost curve to it. Below that the curve is noise and the whole
 * draft is skipped rather than scored badly. */
var MS_MIN_PICKS_PER_DRAFT = 12;

/* Picks per bin when fitting the slot-cost curve. */
var MS_CURVE_BIN = 6;

/* Output scale: league mean 100, one standard deviation 15. */
var MS_INDEX_CENTER = 100;
var MS_INDEX_SCALE = 15;

/* ------------------------------------------------------------------ math */

function msMean(xs) {
  if (!xs || !xs.length) return 0;
  var t = 0;
  for (var i = 0; i < xs.length; i++) t += xs[i];
  return t / xs.length;
}

function msMedian(xs) {
  if (!xs || !xs.length) return 0;
  var s = xs.slice().sort(function (a, b) { return a - b; });
  var m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

function msPstdev(xs) {
  if (!xs || xs.length < 2) return 0;
  var mu = msMean(xs), t = 0;
  for (var i = 0; i < xs.length; i++) t += (xs[i] - mu) * (xs[i] - mu);
  return Math.sqrt(t / xs.length);
}

/* Shrink a per-transaction mean toward zero by sample size.
 *
 * The property that matters, and that the tests pin: this only ever moves
 * a manager TOWARD neutral. |shrunk| <= |mean|, and the sign never flips.
 * Volume therefore cannot manufacture a good score out of mediocre
 * transactions — it can only let a genuinely good (or bad) rate express
 * itself more fully. */
function msShrink(mean, n, k) {
  if (!n || n <= 0) return 0;
  return mean * (n / (n + k));
}

/* {id: value} -> {id: z}. Population sd; all-equal pools yield all zeros. */
function msZScores(byId) {
  var ids = Object.keys(byId || {});
  var vals = ids.map(function (i) { return byId[i]; });
  var mu = msMean(vals), sd = msPstdev(vals);
  var out = {};
  for (var i = 0; i < ids.length; i++) {
    out[ids[i]] = sd > 0 ? (byId[ids[i]] - mu) / sd : 0;
  }
  return out;
}

/* ------------------------------------------------- draft slot-cost curve */

/* Fit "what does a pick at this slot usually return?" from the draft's OWN
 * picks.
 *
 * Deriving the baseline from the same draft is what makes this comparable
 * across formats, league sizes, startup vs rookie drafts and scoring eras
 * without any external calibration: by construction the average pick in a
 * draft scores ~0 surplus, so the metric measures beating your peers in
 * that room, not beating a number we invented.
 *
 * Median-per-bin, then a running minimum so the curve cannot rise as slots
 * get later. Linear interpolation between bin centres; flat outside them.
 */
function msExpectedCurve(pairs, opts) {
  opts = opts || {};
  var binSize = opts.binSize || MS_CURVE_BIN;
  var minPicks = opts.minPicks == null ? MS_MIN_PICKS_PER_DRAFT : opts.minPicks;

  var clean = (pairs || []).filter(function (p) {
    return p && typeof p.slot === 'number' && typeof p.value === 'number' &&
           isFinite(p.slot) && isFinite(p.value);
  }).sort(function (a, b) { return a.slot - b.slot; });

  if (clean.length < minPicks) {
    return { ok: false, reason: 'too few valued picks', n: clean.length, bins: [],
             at: function () { return null; } };
  }

  var bins = [];
  for (var i = 0; i < clean.length; i += binSize) {
    var chunk = clean.slice(i, i + binSize);
    if (!chunk.length) continue;
    bins.push({
      slot: msMedian(chunk.map(function (c) { return c.slot; })),
      value: msMedian(chunk.map(function (c) { return c.value; })),
      n: chunk.length
    });
  }
  /* Enforce non-increasing value as slot grows. */
  for (var j = 1; j < bins.length; j++) {
    if (bins[j].value > bins[j - 1].value) bins[j].value = bins[j - 1].value;
  }

  function at(slot) {
    if (!bins.length) return null;
    if (slot <= bins[0].slot) return bins[0].value;
    if (slot >= bins[bins.length - 1].slot) return bins[bins.length - 1].value;
    for (var k = 1; k < bins.length; k++) {
      if (slot <= bins[k].slot) {
        var a = bins[k - 1], b = bins[k];
        var span = b.slot - a.slot;
        if (span <= 0) return b.value;
        var t = (slot - a.slot) / span;
        return a.value + t * (b.value - a.value);
      }
    }
    return bins[bins.length - 1].value;
  }

  return { ok: true, n: clean.length, bins: bins, at: at };
}

/* ------------------------------------------------------- pick valuation */

function msOrdinal(n) {
  n = Number(n);
  if (n === 1) return '1st';
  if (n === 2) return '2nd';
  if (n === 3) return '3rd';
  return String(n) + 'th';
}

/* KTC publishes picks as "2027 Early 1st" / "Mid" / "Late". A Sleeper trade
 * names only season + round, because which slot the pick lands on is not
 * known until the season ends. "Mid" is therefore the honest expectation
 * for a future pick, and we say so rather than quietly picking Early. */
function msResolvePickValue(picks, season, round) {
  picks = picks || {};
  var ord = msOrdinal(round);
  var mid = season + ' Mid ' + ord;
  if (picks[mid] != null) {
    return { value: picks[mid], basis: 'mid-tier', label: mid };
  }
  var tiers = ['Early', 'Mid', 'Late'];
  var found = [];
  for (var i = 0; i < tiers.length; i++) {
    var key = season + ' ' + tiers[i] + ' ' + ord;
    if (picks[key] != null) found.push(picks[key]);
  }
  if (found.length) {
    return { value: msMean(found), basis: 'season-round average',
             label: season + ' ' + ord };
  }
  /* Season not on the board (too far out, or already passed). Fall back to
   * the average value of that round across every season KTC does publish. */
  var sameRound = [];
  var keys = Object.keys(picks);
  for (var j = 0; j < keys.length; j++) {
    if (keys[j].indexOf(' ' + ord) === keys[j].length - ord.length - 1) {
      sameRound.push(picks[keys[j]]);
    }
  }
  if (sameRound.length) {
    return { value: msMean(sameRound), basis: 'round average (season not on board)',
             label: season + ' ' + ord };
  }
  return { value: null, basis: 'unvalued', label: season + ' ' + ord };
}

/* ------------------------------------------------- point-in-time series */

/* The value history is one aligned series: dates[] plus, per asset key, an
 * array of values with null where the asset was off the board that day.
 *
 * Two lookups matter, and they are deliberately asymmetric in time:
 *
 *   msSeriesValueAt   - the price ON the transaction date. Never looks
 *                       FORWARD; if nothing was recorded on or before that
 *                       day, the answer is "unknown", not "the next one".
 *   msSeriesPeakAfter - the highest price observed on or AFTER that date.
 *
 * The gap between them is what the metric is built on. See msCapture.
 */
function msSeriesIndexAtOrBefore(dates, date) {
  if (!dates || !dates.length || !date) return -1;
  var best = -1;
  for (var i = 0; i < dates.length; i++) {
    if (dates[i] <= date) best = i; else break;
  }
  return best;
}

function msSeriesValueAt(series, key, date, table) {
  if (!series || !series.dates) return null;
  var tbl = series[table || 'sf'] || {};
  var row = tbl[key] || null;
  if (!row) return null;
  var idx = msSeriesIndexAtOrBefore(series.dates, date);
  /* Walk back to the most recent day this asset was actually on the board. */
  for (var i = idx; i >= 0; i--) {
    if (row[i] != null) return { value: row[i], asOf: series.dates[i] };
  }
  return null;
}

function msSeriesPeakAfter(series, key, date, table) {
  if (!series || !series.dates) return null;
  var tbl = series[table || 'sf'] || {};
  var row = tbl[key] || null;
  if (!row) return null;
  var best = null, bestDate = null, seen = false;
  for (var i = 0; i < series.dates.length; i++) {
    if (series.dates[i] < date) continue;
    seen = true;
    if (row[i] == null) continue;
    if (best == null || row[i] > best) { best = row[i]; bestDate = series.dates[i]; }
  }
  if (!seen) return null;   /* nothing observed after this date yet */
  return { value: best, asOf: bestDate, observed: best != null };
}

/* Value capture: what the asset was worth when it changed hands, against
 * the highest it reached afterwards.
 *
 * Why not "value then vs value now": a player's value decays as he ages, so
 * judging a 2023 trade by today's board punishes the passage of time rather
 * than the decision. Why not "highest ever" including before the trade:
 * acquiring a player who already peaked two years earlier would score well
 * on a naive ratio while actually being the purchase of a declining asset.
 *
 * Measuring forward from the transaction fixes both. capture >= 0 always,
 * which is fine because every use is RELATIVE - received against given, or
 * a pick against what its draft slot typically captured. An asset that only
 * declined captures 0; one acquired before a rise captures a lot.
 */
function msCapture(series, key, date, table) {
  var at = msSeriesValueAt(series, key, date, table);
  if (!at) return { evaluable: false, reason: 'no value recorded on or before this date' };
  var peak = msSeriesPeakAfter(series, key, date, table);
  if (!peak) return { evaluable: false, reason: 'too recent - no value recorded since',
                      vAt: at.value, vAtDate: at.asOf };
  var top = (peak.value == null || peak.value < at.value) ? at.value : peak.value;
  return {
    evaluable: true,
    vAt: at.value, vAtDate: at.asOf,
    peak: top, peakDate: peak.asOf || at.asOf,
    capture: top - at.value
  };
}

/* ------------------------------------------------------------- scoring */

/* Score one league.
 *
 * input = {
 *   managers: [{id, name}],
 *   drafts:   [{id, season, label, picks: [{managerId, slot, value, ...}]}],
 *   trades:   [{id, date, sides: [{managerId, received:[asset], given:[asset]}]}],
 *   waivers:  [{managerId, date, value, ...}],
 *   floor:    number|null    // KTC's lowest published value
 * }
 * asset = {kind:'player'|'pick', label, value|null, basis}
 */
function msScoreLeague(input, opts) {
  opts = opts || {};
  var weights = opts.weights || MS_WEIGHTS;
  var shrinkK = opts.shrinkK || MS_SHRINK_K;
  var floor = (input && typeof input.floor === 'number') ? input.floor : 0;

  var managers = (input && input.managers) || [];
  var rows = {};
  managers.forEach(function (m) {
    rows[m.id] = {
      id: m.id, name: m.name,
      draft: { n: 0, total: 0, mean: 0, shrunk: 0, z: 0, available: false },
      trade: { n: 0, total: 0, mean: 0, shrunk: 0, z: 0, available: false },
      waiver: { n: 0, total: 0, mean: 0, shrunk: 0, z: 0, available: false },
      flags: []
    };
  });
  function row(id) { return rows[id] || null; }

  var audit = { picks: [], trades: [], waivers: [] };
  var draftMeta = [];
  var unvalued = 0;
  var notEvaluable = 0;

  /* ---- draft ---- */
  (input.drafts || []).forEach(function (d) {
    var valued = (d.picks || []).filter(function (p) {
      return p.evaluable && typeof p.capture === 'number' && isFinite(p.capture);
    });
    var curve = msExpectedCurve(
      valued.map(function (p) { return { slot: p.slot, value: p.capture }; }),
      { minPicks: opts.minPicksPerDraft }
    );
    draftMeta.push({
      id: d.id, season: d.season, label: d.label,
      nPicks: (d.picks || []).length, nValued: valued.length,
      scored: curve.ok, reason: curve.ok ? null : curve.reason
    });
    if (!curve.ok) return;

    (d.picks || []).forEach(function (p) {
      var r = row(p.managerId);
      if (!r) return;
      if (!p.evaluable || typeof p.capture !== 'number' || !isFinite(p.capture)) {
        notEvaluable++; return;
      }
      var expected = curve.at(p.slot);
      if (expected == null) return;
      var surplus = p.capture - expected;
      r.draft.n += 1;
      r.draft.total += surplus;
      audit.picks.push({
        managerId: p.managerId, draftId: d.id, season: d.season,
        draftLabel: d.label, slot: p.slot, name: p.name, pos: p.pos,
        vAt: p.vAt, vAtDate: p.vAtDate, peak: p.peak, peakDate: p.peakDate,
        capture: p.capture, expected: expected, surplus: surplus,
        basis: p.basis || 'point-in-time', date: p.date || null
      });
    });
  });

  /* ---- trade ---- */
  (input.trades || []).forEach(function (t) {
    var sides = t.sides || [];
    var sideNet = [];
    var partial = false;
    sides.forEach(function (s) {
      var recv = 0, give = 0;
      (s.received || []).forEach(function (a) {
        if (a.evaluable && typeof a.capture === 'number' && isFinite(a.capture)) {
          recv += a.capture;
        } else { partial = true; notEvaluable++; }
      });
      (s.given || []).forEach(function (a) {
        if (a.evaluable && typeof a.capture === 'number' && isFinite(a.capture)) {
          give += a.capture;
        } else { partial = true; notEvaluable++; }
      });
      sideNet.push({ managerId: s.managerId, received: recv, given: give,
                     net: recv - give, assets: s });
    });

    /* Value is conserved in a trade, so the nets MUST sum to zero. When
     * they do not, one of two things is true:
     *
     *  - the trade moved something we do not price in KTC points, which in
     *    practice means FAAB (Sleeper reports it as waiver_budget legs), or
     *  - we mis-parsed the trade.
     *
     * The first is legitimate and gets scored with a flag: cash is not a
     * dynasty asset, so selling a player for FAAB really is a value
     * outflow in these units, but the reader deserves to know cash was
     * involved. The second is a bug, and scoring it would invent a steal
     * out of a parsing error, so it is reported and skipped. Keeping these
     * two apart is the whole point of the check. */
    var netSum = 0;
    sideNet.forEach(function (s) { netSum += s.net; });
    var faabLegs = t.faab || null;
    var hasFaab = !!(faabLegs && faabLegs.length);
    var unbalanced = Math.abs(netSum) > 1 && !hasFaab;

    audit.trades.push({
      id: t.id, date: t.date || null, basis: t.basis || 'current',
      partial: partial, unbalanced: unbalanced, faab: faabLegs,
      imbalance: netSum, sides: sideNet,
      scored: !partial && !unbalanced
    });
    /* A trade with an asset we cannot price would credit the other side
     * with a steal it did not make, so it is reported but not scored. */
    if (partial || unbalanced) return;

    sideNet.forEach(function (s) {
      var r = row(s.managerId);
      if (!r) return;
      r.trade.n += 1;
      r.trade.total += s.net;
    });
  });

  /* ---- waiver ---- */
  (input.waivers || []).forEach(function (w) {
    var r = row(w.managerId);
    if (!r) return;
    if (!w.evaluable || typeof w.capture !== 'number' || !isFinite(w.capture)) {
      notEvaluable++; return;
    }
    /* Capture already answers "did this pickup go on to be worth
     * something": a replacement-level body that never rose captures 0. */
    var surplus = w.capture;
    r.waiver.n += 1;
    r.waiver.total += surplus;
    audit.waivers.push({
      managerId: w.managerId, date: w.date || null, name: w.name, pos: w.pos,
      vAt: w.vAt, vAtDate: w.vAtDate, peak: w.peak, peakDate: w.peakDate,
      capture: w.capture, surplus: surplus,
      faab: w.faab == null ? null : w.faab,
      basis: w.basis || 'point-in-time'
    });
  });

  /* ---- normalise ---- */
  var comps = ['draft', 'trade', 'waiver'];
  var ids = Object.keys(rows);

  comps.forEach(function (c) {
    ids.forEach(function (id) {
      var comp = rows[id][c];
      comp.mean = comp.n ? comp.total / comp.n : 0;
      comp.shrunk = msShrink(comp.mean, comp.n, shrinkK[c]);
      comp.available = comp.n > 0;
    });
    var pool = {};
    ids.forEach(function (id) {
      if (rows[id][c].available) pool[id] = rows[id][c].shrunk;
    });
    /* One participant cannot define a distribution; leave z at 0. */
    var zs = Object.keys(pool).length >= 2 ? msZScores(pool) : {};
    ids.forEach(function (id) {
      rows[id][c].z = zs[id] == null ? 0 : zs[id];
    });
  });

  /* Renormalise weights over the components the league actually has, so a
   * league that never trades is not scored on 65% of the scale. */
  var live = comps.filter(function (c) {
    var n = 0;
    ids.forEach(function (id) { if (rows[id][c].available) n++; });
    return n >= 2;
  });
  var wsum = 0;
  live.forEach(function (c) { wsum += weights[c]; });
  var effective = {};
  comps.forEach(function (c) {
    effective[c] = (wsum > 0 && live.indexOf(c) >= 0) ? weights[c] / wsum : 0;
  });

  var out = ids.map(function (id) {
    var r = rows[id];
    var composite = 0;
    comps.forEach(function (c) { composite += effective[c] * r[c].z; });
    r.composite = composite;
    r.index = MS_INDEX_CENTER + MS_INDEX_SCALE * composite;
    comps.forEach(function (c) {
      if (!r[c].available && effective[c] > 0) {
        r.flags.push('no ' + c + ' activity — scored as league average');
      }
    });
    if (r.draft.available && r.draft.n < MS_SHRINK_K.draft) {
      r.flags.push('only ' + r.draft.n + ' scored picks — heavily shrunk');
    }
    return r;
  });

  out.sort(function (a, b) { return b.index - a.index || a.name.localeCompare(b.name); });
  out.forEach(function (r, i) { r.rank = i + 1; });

  return {
    managers: out,
    drafts: draftMeta,
    audit: audit,
    weights: effective,
    meta: {
      unvaluedAssets: unvalued,
      notEvaluableAssets: notEvaluable,
      liveComponents: live,
      floor: floor,
      nTrades: (input.trades || []).length,
      nTradesScored: audit.trades.filter(function (t) { return t.scored; }).length,
      nTradesUnbalanced: audit.trades.filter(function (t) { return t.unbalanced; }).length,
      nTradesWithFaab: audit.trades.filter(function (t) { return !!(t.faab && t.faab.length); }).length,
      nWaivers: audit.waivers.length,
      nPicksScored: audit.picks.length
    }
  };
}

/* ============================================================
 * REALIZED PRODUCTION — the second lens
 * ============================================================
 *
 * Everything above prices a transaction against the KeepTradeCut market:
 * it answers "was this a good bet at the time". It cannot answer the
 * question an owner actually remembers -- "did this work out". A trade for
 * a back who then carries you to a title and a trade for a back who then
 * tears an achilles can price identically on the day they are made.
 *
 * This half answers the second question from ground truth: the points the
 * acquired players actually scored for the manager who acquired them,
 * taken from Sleeper's own weekly matchup records.
 *
 * Why this source and not a stat line
 * -----------------------------------
 * ``/league/{id}/matchups/{week}`` returns, per roster, a
 * ``players_points`` map of ``player_id -> points``. Those numbers are
 * computed by Sleeper with THAT LEAGUE'S OWN scoring settings. This league,
 * for instance, is half-PPR with a tight-end bonus, so a generic PPR stat
 * feed would be wrong for it in a way that quietly favours receivers. Using
 * the league's own numbers means the realized lens is exact rather than
 * approximate, and needs no scoring configuration of its own.
 *
 * Ownership is read from the same records, not inferred from dates
 * ----------------------------------------------------------------
 * The matchup record for a week lists the roster each player was actually
 * on that week. So "was this player still on the acquiring roster in week
 * 14" is a lookup, not a calculation. That disposes of every awkward case
 * in one move: a player traded on again, dropped, stashed on someone else's
 * taxi squad, or acquired the day before kickoff. We never count a week a
 * player did not spend on the acquiring roster, and we never have to guess
 * what a transaction date implies about a lineup lock.
 *
 * The normalisation problem
 * -------------------------
 * Raw points make a week-2 trade look better than a week-12 trade purely
 * because it had ten more weeks to accumulate. Two defences, both reported:
 *
 *   - points per week held, which is a rate and so span-independent; and
 *   - PAR, points above replacement, where "replacement" is the MEDIAN
 *     score of a started player at the same position in the same week of
 *     the same league.
 *
 * PAR is the headline. Being position-relative it does not reward acquiring
 * a quarterback over a running back for the structural reason that
 * quarterbacks score more, and being week-relative it self-normalises for
 * span: a quiet week contributes near zero on both sides rather than
 * padding whoever held the ball longest. It is also league-relative, so a
 * high-scoring format does not inflate it.
 *
 * ALL ROSTERED production is the basis, by owner decision
 * -------------------------------------------------------
 * Sleeper gives us both: `players_points` covers the whole roster, and
 * `starters` says who was in the lineup. (Verified live that the two
 * agree: summing `players_points` over `starters` reproduces
 * `starters_points` and the roster's `points` exactly.)
 *
 * This counts ALL rostered production. The reasoning is that the thing
 * being graded is the ACQUISITION, not the manager's weekly lineup card.
 * Trading for a player who goes on to produce is a trade skill; leaving
 * him on your bench while he does it is a lineup skill, and a different
 * one. Charging the trade for a benching would conflate the two and
 * would punish, for instance, acquiring a stash who breaks out while
 * blocked on your roster.
 *
 * The honest caveat, stated on the page and not only here: bench points
 * did not directly win anybody a game. A manager who acquires a producer
 * and never starts him gets full credit from this measure for a benefit
 * he never actually banked. That is a deliberate consequence of scoring
 * the acquisition in isolation -- lineup management is simply not
 * measured anywhere on this page. Started production is therefore carried
 * alongside every figure and rendered next to it, so the gap between the
 * two remains visible even though only the total is scored.
 *
 * The baseline population must match
 * ----------------------------------
 * This is the part that cannot be left alone when the basis changes. PAR
 * subtracts a replacement level, and that level has to be drawn from the
 * same population being measured, or the subtraction is measuring one
 * thing with another thing's ruler. Scoring every rostered week against a
 * STARTED player's median would charge a benched or bye-week player a
 * starter's replacement level for a week he was never asked to play, and
 * would manufacture large negative PAR out of nothing.
 *
 * So replacement is now the median among ALL ROSTERED players at that
 * position that week. Measured on a real four-season league, that moves
 * the RB bar from 10.91 to 3.49 points a week, because roughly a third of
 * rostered players score zero in any given week (byes, inactives, deep
 * stashes). PAR values are correspondingly larger than they would be on a
 * starter basis -- the same Derrick Henry trade reads +177.2 rather than
 * +103.9. That is a change of scale, not of accuracy: every trade is
 * measured against the same bar, and the comparison between them is what
 * the number is for.
 *
 * Where that bar stops meaning anything
 * -------------------------------------
 * A median over the rostered pool is not stable everywhere, and measuring
 * it on real data rather than assuming found two places it breaks:
 *
 *  - Quarterbacks in bye-heavy weeks. Most rostered QBs are backups who
 *    never play, so in some weeks more than half the pool scores zero and
 *    the median goes to 0.00. PAR would then equal raw points and the
 *    positional normalisation would silently switch itself off in exactly
 *    the weeks a star QB looks most impressive.
 *  - Weeks the league did not actually play (see msLeagueLastWeek).
 *
 * Rather than let PAR quietly become raw points, a week whose positional
 * median falls below MS_MIN_BASELINE is treated as having NO meaningful
 * replacement level: production still counts toward the totals, but that
 * week contributes nothing to PAR and is excluded from its week count.
 * `parWeeks` versus `weeksHeld` is therefore the honest coverage figure,
 * and the page reports it when they differ.
 */

function msWeekId(season, week) {
  return String(season) + ':' + String(week);
}

/* Position of a player, via the values artifact, with a caller-supplied
 * lookup so the core stays free of globals and the tests can inject one. */
function msPosLookup(map) {
  return function (playerId) {
    var e = map ? map[String(playerId)] : null;
    if (!e) return '';
    return (typeof e === 'string' ? e : e[2]) || '';
  };
}

/* Fold raw Sleeper matchup responses into a chronological ledger.
 *
 * `weeksIn` is [{ season, week, leagueId, matchups: [...] }] in any order;
 * `rosterToUser` maps "leagueId:rosterId" -> userId, because roster ids are
 * reassigned between seasons and user ids are not. Ownership is therefore
 * recorded against the manager, which is what survives the chain.
 */
function msBuildLedger(weeksIn, rosterToUser) {
  var weeks = {};
  var keys = [];
  (weeksIn || []).forEach(function (entry) {
    if (!entry) return;
    var id = msWeekId(entry.season, entry.week);
    var bucket = weeks[id];
    if (!bucket) {
      bucket = weeks[id] = { id: id, season: String(entry.season),
                             week: Number(entry.week), owner: {},
                             pts: {}, start: {} };
      keys.push(id);
    }
    (entry.matchups || []).forEach(function (m) {
      if (!m) return;
      var uid = (rosterToUser || {})[entry.leagueId + ':' + m.roster_id];
      if (!uid) uid = 'roster:' + entry.leagueId + ':' + m.roster_id;
      var pp = m.players_points || {};
      var starters = {};
      (m.starters || []).forEach(function (s) { starters[String(s)] = true; });
      /* `players` is the full roster; `players_points` covers it too, but a
       * roster entry with no points row still proves ownership, so both are
       * walked. Ownership and scoring are separate facts. */
      (m.players || []).forEach(function (pid) {
        bucket.owner[String(pid)] = uid;
      });
      for (var pid in pp) {
        if (!Object.prototype.hasOwnProperty.call(pp, pid)) continue;
        bucket.owner[String(pid)] = uid;
        var v = pp[pid];
        if (typeof v === 'number' && isFinite(v)) bucket.pts[String(pid)] = v;
        if (starters[String(pid)]) bucket.start[String(pid)] = true;
      }
    });
  });
  /* Drop weeks that have not been played.
   *
   * Sleeper answers `matchups/{week}` for the WHOLE season the moment it
   * opens: an unplayed week comes back as a full set of roster objects with
   * every `players_points` value at 0, indistinguishable at a glance from a
   * real week. Verified against the live API mid-season: 2026 weeks 3-18
   * returned twelve rosters each and not one non-zero score.
   *
   * Left in, those phantom weeks are counted as weeks held. That does not
   * change any points total, but it wrecks every rate: a player acquired
   * last November showed 26 weeks held in September, so points-per-week and
   * PAR-per-week were diluted by a factor of three by weeks that do not
   * exist yet. A week counts only once somebody has actually scored in it.
   */
  var played = keys.filter(function (id) {
    var w = weeks[id];
    for (var pid in w.pts) {
      if (w.pts[pid]) return true;
    }
    delete weeks[id];
    return false;
  });
  played.sort(function (a, b) {
    var A = a.split(':'), B = b.split(':');
    return (Number(A[0]) - Number(B[0])) || (Number(A[1]) - Number(B[1]));
  });
  return { order: played, weeks: weeks };
}

function msWeekIndex(ledger, season, week) {
  var id = msWeekId(season, week);
  var order = (ledger && ledger.order) || [];
  for (var i = 0; i < order.length; i++) if (order[i] === id) return i;
  return -1;
}

/* Replacement level per week per position: the median score among ALL
 * ROSTERED players at that position in that league that week.
 *
 * Rostered rather than started, because production is scored over every
 * rostered week and the two sides of the subtraction have to come from
 * the same population -- see the header note. Median rather than mean
 * because a single 45-point week should barely move the bar for what a
 * typical asset at that position was giving you. */
function msBaselines(ledger, posOf) {
  var out = {};
  var order = (ledger && ledger.order) || [];
  var weeks = (ledger && ledger.weeks) || {};
  order.forEach(function (id) {
    var w = weeks[id];
    if (!w) return;
    var buckets = {};
    for (var pid in w.pts) {
      var pos = posOf(pid);
      if (!pos) continue;
      var v = w.pts[pid];
      if (typeof v !== 'number' || !isFinite(v)) continue;
      (buckets[pos] = buckets[pos] || []).push(v);
    }
    var per = {};
    for (var p in buckets) {
      var med = msMedian(buckets[p]);
      /* Degenerate pool: no meaningful replacement level this week. Left
       * undefined so msRealizedAsset skips it rather than crediting the
       * player with his entire score. */
      if (med >= MS_MIN_BASELINE) per[p] = med;
    }
    out[id] = per;
  });
  return out;
}

/* The last week this league actually played.
 *
 * Sleeper answers `matchups/18` for a league whose season ended at week
 * 17, and it answers with real NFL scoring attached to a stale carried-
 * over lineup. Verified across three seasons of a real league: week 18
 * returned 281-318 rostered entries and exactly 108 flagged starters every
 * time -- the same 12 x 9 lineup as the previous week, untouched, because
 * nobody was setting lineups any more. Counting it credits trades with
 * production that decided nothing.
 *
 * The league states where its own season ends: `playoff_week_start` plus
 * one round per doubling of the playoff field. Absent settings fall back
 * to 18, which is the old behaviour and no worse than it was.
 */
function msLeagueLastWeek(league) {
  var s = (league && league.settings) || {};
  var start = Number(s.playoff_week_start || 0);
  if (!start || start < 1) return 18;
  var teams = Number(s.playoff_teams || 0);
  var rounds = 1;
  if (teams > 1) rounds = Math.ceil(Math.log(teams) / Math.LN2);
  if (!isFinite(rounds) || rounds < 1) rounds = 1;
  return Math.min(18, start + rounds - 1);
}

/* Tenure + production for one acquired player, from `startIdx` forward.
 *
 * `graceWeeks` exists because a trade agreed after a lineup lock shows up on
 * the new roster the following week. Before the player is first seen on the
 * acquiring roster we tolerate a short wait; once the tenure has started,
 * the first week they are elsewhere (or on nobody) ends it. Without the
 * grace we would report zero for a Saturday trade; without the hard stop
 * after first sighting we would credit a manager for a player they had
 * already traded on.
 */
function msRealizedAsset(ledger, baselines, posOf, startIdx, playerId, userId, opts) {
  opts = opts || {};
  var grace = opts.graceWeeks == null ? 3 : opts.graceWeeks;
  var order = (ledger && ledger.order) || [];
  var weeks = (ledger && ledger.weeks) || {};
  var pid = String(playerId);
  var pos = posOf(pid) || '';

  var out = {
    playerId: pid, pos: pos, weekIds: [],
    weeksHeld: 0, starts: 0,
    /* ptsTotal is the scored basis; ptsStarted is disclosed beside it so
     * the lineup story stays visible even though it is not scored. */
    ptsTotal: 0, ptsStarted: 0, benchPts: 0,
    par: 0, parWeeks: 0, parAvailable: false,
    firstWeek: null, lastWeek: null, departedWeek: null,
    arrived: false, truncated: false
  };
  if (startIdx < 0) return out;

  var waited = 0;
  for (var i = startIdx; i < order.length; i++) {
    var id = order[i];
    var w = weeks[id];
    if (!w) continue;                       /* week not fetched at all */
    var owner = w.owner[pid];
    if (owner !== userId) {
      if (out.arrived) { out.departedWeek = id; break; }
      waited++;
      if (waited > grace) break;            /* never actually delivered */
      continue;
    }
    if (!out.arrived) { out.arrived = true; out.firstWeek = id; }
    out.lastWeek = id;
    out.weekIds.push(id);
    out.weeksHeld++;
    var v = w.pts[pid];
    if (typeof v === 'number' && isFinite(v)) {
      out.ptsTotal += v;
      if (w.start[pid]) { out.ptsStarted += v; out.starts++; }
      /* PAR accrues over EVERY rostered week, started or not, because the
       * acquisition is what is being graded. The baseline is the rostered
       * population's median, so a benched week is compared against other
       * rostered players rather than against starters. */
      var base = pos ? ((baselines || {})[id] || {})[pos] : null;
      if (typeof base === 'number' && isFinite(base)) {
        out.par += (v - base);
        out.parWeeks++;
      }
    }
  }
  /* Still on the roster at the end of the record: production is ongoing,
   * not final. The page says so rather than presenting a running total as
   * a settled result. */
  if (out.arrived && out.departedWeek == null &&
      out.lastWeek === order[order.length - 1]) {
    out.truncated = true;
  }
  out.parAvailable = out.parWeeks > 0;
  out.benchPts = out.ptsTotal - out.ptsStarted;
  /* Rates follow the scored basis, so ppw and PAR/week are comparable. */
  out.ppw = out.weeksHeld ? out.ptsTotal / out.weeksHeld : 0;
  out.ppwStarted = out.weeksHeld ? out.ptsStarted / out.weeksHeld : 0;
  out.parPerWeek = out.weeksHeld ? out.par / out.weeksHeld : 0;
  return out;
}

/* Where a player ranked at their own position, by total points, among every
 * player rostered in this league over the same span. This is what makes a
 * number like "147 points" legible: 147 is meaningless until you know it
 * was RB2 over those weeks. */
function msPosRankInSpan(ledger, posOf, weekIds, pos, playerId) {
  var totals = {};
  var weeks = (ledger && ledger.weeks) || {};
  (weekIds || []).forEach(function (id) {
    var w = weeks[id];
    if (!w) return;
    for (var pid in w.pts) {
      if (posOf(pid) !== pos) continue;
      var v = w.pts[pid];
      if (typeof v !== 'number' || !isFinite(v)) continue;
      totals[pid] = (totals[pid] || 0) + v;
    }
  });
  var arr = [];
  for (var k in totals) arr.push({ id: k, pts: totals[k] });
  arr.sort(function (a, b) { return b.pts - a.pts || a.id.localeCompare(b.id); });
  var rank = 0;
  for (var i = 0; i < arr.length; i++) {
    if (arr[i].id === String(playerId)) { rank = i + 1; break; }
  }
  return { rank: rank, of: arr.length,
           points: totals[String(playerId)] || 0, pos: pos };
}

/* Realized view of one trade.
 *
 * Draft picks are deliberately NOT given a realized value. Sleeper's trade
 * record names a traded pick by (season, round, original roster) but the
 * draft-results feed does not expose which selection descended from which
 * traded slot, so attributing a drafted player's production back to the
 * pick that bought him would require a guess. Guessing here would be
 * indistinguishable from evidence on the page, so picks are reported as
 * unattributed and the trade is flagged `partial`. This is exactly the case
 * the market lens already handles well -- KTC prices picks directly -- which
 * is the clearest argument for showing both lenses rather than one blended
 * number.
 */
function msRealizedTrade(trade, ledger, baselines, posOf, opts) {
  /* Deriving these when absent rather than treating a missing table as an
   * empty one: a null here would otherwise yield PAR 0 everywhere, which
   * reads as "replacement level" rather than "not computed" and would be
   * indistinguishable from a real result on the page. */
  if (!baselines) baselines = msBaselines(ledger, posOf);
  var startIdx = msWeekIndex(ledger, trade.season, trade.week);
  var sides = (trade.sides || []).map(function (s) {
    var assets = [], picks = 0;
    (s.received || []).forEach(function (a) {
      if (a.kind === 'pick' || !a.playerId) { picks++; return; }
      var r = msRealizedAsset(ledger, baselines, posOf, startIdx,
                              a.playerId, s.managerId, opts);
      r.label = a.label;
      r.rank = r.weeksHeld && r.pos
        ? msPosRankInSpan(ledger, posOf, r.weekIds, r.pos, a.playerId)
        : null;
      assets.push(r);
    });
    var tot = { ptsTotal: 0, ptsStarted: 0, par: 0, weeksHeld: 0, starts: 0 };
    var anyPar = false, ongoing = false;
    assets.forEach(function (r) {
      tot.ptsTotal += r.ptsTotal;
      tot.ptsStarted += r.ptsStarted;
      tot.par += r.par;
      tot.starts += r.starts;
      if (r.weeksHeld > tot.weeksHeld) tot.weeksHeld = r.weeksHeld;
      if (r.parAvailable) anyPar = true;
      if (r.truncated) ongoing = true;
    });
    return {
      managerId: s.managerId, assets: assets, picksUnattributed: picks,
      ptsTotal: tot.ptsTotal, ptsStarted: tot.ptsStarted,
      benchPts: tot.ptsTotal - tot.ptsStarted, par: tot.par,
      starts: tot.starts, weeksHeld: tot.weeksHeld,
      parPerWeek: tot.weeksHeld ? tot.par / tot.weeksHeld : 0,
      ppw: tot.weeksHeld ? tot.ptsTotal / tot.weeksHeld : 0,
      parAvailable: anyPar, ongoing: ongoing
    };
  });

  var measurable = sides.length >= 2 && sides.some(function (s) {
    return s.assets.length > 0;
  });
  var partial = sides.some(function (s) { return s.picksUnattributed > 0; });
  var ongoing = sides.some(function (s) { return s.ongoing; });

  /* Net is stated from each side's own point of view: what I got minus what
   * the other side got. Unlike the market lens this is NOT guaranteed
   * zero-sum -- both managers can genuinely win a trade, because points are
   * produced rather than exchanged. That is a property of the measure, not
   * a bug, and the page says so. */
  sides.forEach(function (s) {
    var others = sides.filter(function (o) { return o !== s; });
    var oppPar = 0, oppStarted = 0, oppTotal = 0, oppPpw = 0;
    others.forEach(function (o) {
      oppPar += o.par; oppStarted += o.ptsStarted;
      oppTotal += o.ptsTotal; oppPpw += o.parPerWeek;
    });
    s.netPar = s.par - oppPar;
    /* All-rostered basis, matching what is scored. The started-only net is
     * kept for disclosure so the two can be compared directly. */
    s.netPts = s.ptsTotal - oppTotal;
    s.netStarted = s.ptsStarted - oppStarted;
    s.netParPerWeek = s.parPerWeek - oppPpw;
  });

  return {
    id: trade.id, date: trade.date || null,
    season: trade.season, week: trade.week,
    startIdx: startIdx, measurable: measurable && startIdx >= 0,
    partial: partial, ongoing: ongoing, sides: sides
  };
}

/* Annotate a scored result in place with the realized lens.
 *
 * Kept separate from msScoreLeague on purpose. The Manager Score index
 * stays exactly what it was -- the owner's feedback was that the score
 * reads well for drafts and waivers, so silently folding a second measure
 * into the headline would break something that already works. Realized
 * production is displayed beside the market view, never averaged into it.
 */
function msAttachRealized(result, input, ledger, posOf, opts) {
  if (!result || !result.audit) return result;
  var baselines = msBaselines(ledger, posOf);
  var byId = {};
  (input.trades || []).forEach(function (t) {
    byId[String(t.id)] = msRealizedTrade(t, ledger, baselines, posOf, opts);
  });
  var measurable = 0;
  result.audit.trades.forEach(function (t) {
    var r = byId[String(t.id)];
    if (!r) return;
    t.realized = r;
    if (r.measurable) measurable++;
  });
  /* Per-manager rollup, reported beside the index rather than inside it. */
  var perMgr = {};
  result.audit.trades.forEach(function (t) {
    if (!t.realized || !t.realized.measurable) return;
    t.realized.sides.forEach(function (s) {
      var m = perMgr[s.managerId] ||
        (perMgr[s.managerId] = { n: 0, par: 0, pts: 0, ptsStarted: 0, netPar: 0 });
      m.n++; m.par += s.par; m.pts += s.ptsTotal;
      m.ptsStarted += s.ptsStarted; m.netPar += s.netPar;
    });
  });
  (result.managers || []).forEach(function (m) {
    var p = perMgr[m.id];
    m.realized = p
      ? { n: p.n, par: p.par, pts: p.pts, ptsStarted: p.ptsStarted,
          netPar: p.netPar, netParPerTrade: p.n ? p.netPar / p.n : 0 }
      : { n: 0, par: 0, pts: 0, ptsStarted: 0, netPar: 0, netParPerTrade: 0 };
  });
  result.meta = result.meta || {};
  result.meta.nTradesRealized = measurable;
  return result;
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    MS_WEIGHTS: MS_WEIGHTS, MS_SHRINK_K: MS_SHRINK_K,
    MS_INDEX_CENTER: MS_INDEX_CENTER, MS_INDEX_SCALE: MS_INDEX_SCALE,
    msMean: msMean, msMedian: msMedian, msPstdev: msPstdev,
    msShrink: msShrink, msZScores: msZScores,
    msExpectedCurve: msExpectedCurve, msOrdinal: msOrdinal,
    msSeriesValueAt: msSeriesValueAt, msSeriesPeakAfter: msSeriesPeakAfter,
    msCapture: msCapture,
    msResolvePickValue: msResolvePickValue, msScoreLeague: msScoreLeague,
    MS_MIN_BASELINE: MS_MIN_BASELINE,
    msWeekId: msWeekId, msPosLookup: msPosLookup,
    msLeagueLastWeek: msLeagueLastWeek,
    msBuildLedger: msBuildLedger, msWeekIndex: msWeekIndex,
    msBaselines: msBaselines, msRealizedAsset: msRealizedAsset,
    msPosRankInSpan: msPosRankInSpan, msRealizedTrade: msRealizedTrade,
    msAttachRealized: msAttachRealized
  };
}
"""


# ---------------------------------------------------------------------------
# Impure UI half — Sleeper reads, artifact loads, rendering
# ---------------------------------------------------------------------------

MANAGERSCORE_UI_JS = r"""
/* ============================================================
 * Manager Score — UI layer.
 * Sleeper is read live from the browser, exactly as My Team and
 * the Roster Reel do: no league data reaches this site's server,
 * because there isn't one - it is a static GitHub Pages build.
 * ============================================================ */

var MSX = {
  values: null,        /* managerscore_values.json */
  series: null,        /* managerscore_series.json - the dated value history */
  result: null,
  leagueChain: [],
  ledger: null,        /* weekly ownership + points, the realized lens */
  posOf: null,
  loadError: null
};

/* Prefixed like every other global in this file. It must be: this
 * script now shares a document with reel.js, which declares its own
 * top-level `const SLEEPER`, and two same-named top-level bindings in
 * one document are a SyntaxError that takes the whole page down.
 * scripts/check_site_js.py checks the concatenation for exactly this. */
var MS_SLEEPER = 'https://api.sleeper.app/v1';

function msEsc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function msEl(id) { return document.getElementById(id); }

function msStatus(html, kind) {
  var box = msEl('ms-status');
  if (!box) return;
  box.style.display = html ? 'block' : 'none';
  box.className = 'callout' + (kind ? ' callout-' + kind : '');
  box.innerHTML = html || '';
}

function msGetJSON(url) {
  return fetch(url).then(function (r) {
    if (!r.ok) throw new Error(url + ' -> HTTP ' + r.status);
    return r.json();
  });
}

/* Sleeper endpoints that legitimately 404 (a league with no drafts, a week
 * with no transactions) must not abort the whole build. */
function msGetJSONSoft(url, fallback) {
  return fetch(url).then(function (r) {
    if (!r.ok) return fallback;
    return r.json().catch(function () { return fallback; });
  }).catch(function () { return fallback; });
}

/* ------------------------------------------------- polite Sleeper reads
 *
 * Sleeper's API is public, unauthenticated and free, and this page can ask
 * it for a lot: a four-season dynasty chain is four league reads, four
 * roster/user/draft reads, ~19 transaction pages per season and up to 18
 * matchup pages per season. Fired naively and repeated on every re-run,
 * that is inconsiderate to an API nobody is paying for.
 *
 * Three measures, in order of how much they save:
 *
 *  1. CACHE. Every GET goes through a cache keyed by URL. Within a page
 *     load it is a plain object; across reloads it is sessionStorage, so
 *     re-scoring the same league costs zero requests. Completed seasons
 *     are marked immutable and never expire -- a 2023 box score is not
 *     going to change. Anything touching the current season carries a
 *     short TTL so an in-progress week still refreshes.
 *  2. BOUND THE WEEKS. The current season is only asked for weeks up to
 *     the one Sleeper says is live, rather than all 18. Mid-September that
 *     is 15 requests saved per run.
 *  3. LIMIT CONCURRENCY. Requests run through a small pool instead of one
 *     enormous Promise.all, so we are never more than a handful of
 *     connections deep.
 *
 * sessionStorage is used rather than localStorage deliberately: league
 * data should not outlive the tab, and the quota is a hard limit we can
 * hit. Every storage call is wrapped -- a full or disabled store degrades
 * to "no persistence", never to an error.
 */

var MS_CACHE_VERSION = 'v1';
var MS_CACHE_TTL_MS = 30 * 60 * 1000;      /* 30 min for mutable reads */
var MS_MAX_CONCURRENCY = 6;

var msMemCache = {};

function msCacheKey(url) { return 'msc:' + MS_CACHE_VERSION + ':' + url; }

function msCacheRead(url, immutable) {
  var hit = msMemCache[url];
  if (hit) {
    if (immutable || (Date.now() - hit.t) < MS_CACHE_TTL_MS) return hit.v;
  }
  try {
    if (typeof sessionStorage === 'undefined') return undefined;
    var raw = sessionStorage.getItem(msCacheKey(url));
    if (!raw) return undefined;
    var rec = JSON.parse(raw);
    if (!rec || typeof rec.t !== 'number') return undefined;
    if (!immutable && (Date.now() - rec.t) >= MS_CACHE_TTL_MS) return undefined;
    msMemCache[url] = rec;
    return rec.v;
  } catch (e) { return undefined; }
}

/* Drop everything cached. Exposed so a user can force a refresh, and so
 * tests can run successive scenarios against the same URLs. */
function msCacheClear() {
  msMemCache = {};
  try {
    if (typeof sessionStorage === 'undefined') return;
    var kill = [];
    for (var i = 0; i < sessionStorage.length; i++) {
      var k = sessionStorage.key(i);
      if (k && k.indexOf('msc:') === 0) kill.push(k);
    }
    kill.forEach(function (k) { sessionStorage.removeItem(k); });
  } catch (e) { /* storage unavailable: memory clear is enough */ }
}

function msCacheWrite(url, value) {
  var rec = { t: Date.now(), v: value };
  msMemCache[url] = rec;
  try {
    if (typeof sessionStorage === 'undefined') return;
    sessionStorage.setItem(msCacheKey(url), JSON.stringify(rec));
  } catch (e) {
    /* Quota exceeded or storage disabled. The in-memory cache still holds,
     * so this run is unaffected; only cross-reload reuse is lost. */
  }
}

/* Cached soft GET. `opts.immutable` marks data that cannot change again
 * (a completed season), `opts.distil` shrinks a payload before it is
 * cached so a big response does not blow the storage quota. */
function msGetCached(url, fallback, opts) {
  opts = opts || {};
  var cached = msCacheRead(url, !!opts.immutable);
  if (cached !== undefined) return Promise.resolve(cached);
  return msGetJSONSoft(url, fallback).then(function (v) {
    var keep = opts.distil ? opts.distil(v) : v;
    msCacheWrite(url, keep);
    return keep;
  });
}

/* Run `fn` over `items` at most `limit` at a time, preserving order. */
function msMapLimit(items, limit, fn) {
  var out = new Array(items.length);
  var next = 0;
  function worker() {
    if (next >= items.length) return Promise.resolve();
    var i = next++;
    return Promise.resolve(fn(items[i], i)).then(function (v) {
      out[i] = v;
      return worker();
    });
  }
  var pool = [];
  for (var w = 0; w < Math.min(limit, items.length); w++) pool.push(worker());
  return Promise.all(pool).then(function () { return out; });
}

/* Sleeper's own view of the live season and week, so the current season is
 * not asked for weeks that have not happened. Cached; failure is
 * non-fatal and simply means we fall back to asking for all 18. */
function msNflState() {
  return msGetCached(MS_SLEEPER + '/state/nfl', null, {}).then(function (s) {
    return s || null;
  });
}

/* ------------------------------------------------------------ artifacts */

function msLoadValues() {
  return msGetJSON('managerscore_values.json').then(function (v) {
    MSX.values = v;
    return msGetJSONSoft('managerscore_series.json', null).then(function (s) {
      MSX.series = s;
      return v;
    });
  }).catch(function (e) {
    MSX.values = null;
    MSX.loadError = String(e && e.message || e);
    return null;
  });
}

function msHistoryDates() {
  var s = MSX.series;
  return (s && s.dates) || [];
}

function msPlayerEntry(sleeperId) {
  var v = MSX.values;
  if (!v || !v.by_sleeper) return null;
  return v.by_sleeper[String(sleeperId)] || null;
}

/* [value, name, position, ktc_id] */
function msCaptureForPlayer(sleeperId, date, fallbackName) {
  var entry = msPlayerEntry(sleeperId);
  var name = (entry && entry[1]) || fallbackName || ('Sleeper #' + sleeperId);
  var pos = (entry && entry[2]) || '';
  var ktcId = entry && entry[3] != null ? String(entry[3]) : null;

  if (!ktcId) {
    return { evaluable: false, name: name, pos: pos,
             reason: 'not on the KeepTradeCut board' };
  }
  var c = msCapture(MSX.series, ktcId, date, 'sf');
  c.name = name; c.pos = pos;
  return c;
}

function msCaptureForPick(season, round, date) {
  var picks = (MSX.series && MSX.series.picks) || {};
  /* Resolve the label against whichever pick rows the archive actually
   * carries, then capture on that label's own series. */
  var r = msResolvePickValue(msPickSnapshotAt(date), String(season), round);
  var label = r.label;
  var key = null;
  if (picks[season + ' Mid ' + msOrdinal(round)]) key = season + ' Mid ' + msOrdinal(round);
  else if (picks[label]) key = label;
  if (!key) {
    return { evaluable: false, name: label, pos: 'PICK',
             reason: 'pick not on the archived board' };
  }
  var c = msCapture(MSX.series, key, date, 'picks');
  c.name = key; c.pos = 'PICK';
  return c;
}

/* Values on the nearest archived date at or before `date`, used only to
 * resolve which pick label exists; capture then runs on the full series. */
function msPickSnapshotAt(date) {
  var s = MSX.series;
  if (!s || !s.dates) return {};
  var idx = msSeriesIndexAtOrBefore(s.dates, date);
  if (idx < 0) return {};
  var out = {};
  var picks = s.picks || {};
  for (var k in picks) {
    if (picks[k] && picks[k][idx] != null) out[k] = picks[k][idx];
  }
  return out;
}

/* --------------------------------------------------------- sleeper reads */

function msMsToDate(ms) {
  var n = Number(ms);
  if (!n || !isFinite(n)) return null;
  if (n < 1e12) n = n * 1000;           /* seconds -> ms */
  var d = new Date(n);
  if (isNaN(d.getTime())) return null;
  return d.toISOString().slice(0, 10);
}

/* Walk previous_league_id back through prior seasons. Dynasty leagues are
 * the same managers year over year, and a manager's draft record is mostly
 * in seasons already gone, so ignoring the chain would score one season
 * and call it a career. */
function msLeagueChain(leagueId, maxHops) {
  var chain = [];
  var hops = maxHops == null ? 12 : maxHops;
  function step(id) {
    if (!id || chain.length >= hops) return Promise.resolve(chain);
    return msGetCached(MS_SLEEPER + '/league/' + id, null, {}).then(function (lg) {
      if (!lg || !lg.league_id) return chain;
      chain.push(lg);
      return step(lg.previous_league_id);
    });
  }
  return step(leagueId);
}

function msFetchLeagueData(leagueId, includeHistory) {
  return msNflState().then(function (state) {
  return (includeHistory ? msLeagueChain(leagueId) :
          msGetCached(MS_SLEEPER + '/league/' + leagueId, null, {})
            .then(function (lg) { return lg ? [lg] : []; })
  ).then(function (chain) {
    MSX.leagueChain = chain;
    if (!chain.length) throw new Error('League ' + leagueId + ' not found on Sleeper.');
    /* Seasons are walked one at a time rather than all at once: each one
     * already fans out internally, and stacking four of those on top of
     * each other is exactly the burst this is meant to avoid. */
    return msMapLimit(chain, 1, function (lg) {
      var id = lg.league_id;
      return Promise.all([
        msGetCached(MS_SLEEPER + '/league/' + id + '/users', [], {}),
        msGetCached(MS_SLEEPER + '/league/' + id + '/rosters', [], {}),
        msGetCached(MS_SLEEPER + '/league/' + id + '/drafts', [], {}),
        msFetchTransactions(id, lg.season, state),
        msFetchMatchups(id, lg.season, state, lg)
      ]).then(function (parts) {
        return { league: lg, users: parts[0] || [], rosters: parts[1] || [],
                 drafts: parts[2] || [], transactions: parts[3] || [],
                 matchupWeeks: parts[4] || [] };
      });
    });
  }).then(function (seasons) {
    return Promise.all(seasons.map(function (s) {
      return Promise.all((s.drafts || []).map(function (d) {
        var did = d.draft_id || d.id;
        if (!did) return Promise.resolve({ draft: d, picks: [] });
        return msGetCached(MS_SLEEPER + '/draft/' + did + '/picks', [], {})
          .then(function (p) { return { draft: d, picks: p || [] }; });
      })).then(function (drafts) { s.draftPicks = drafts; return s; });
    }));
  });
  });
}

/* Weekly matchup records for one league season.
 *
 * This is the realized lens's entire data source. Each week's response
 * carries, per roster, `players_points` (the league's OWN scoring applied
 * to every rostered player, bench included) and `starters`. Weeks that do
 * not exist yet come back empty and are simply absent from the ledger,
 * which is why an in-season league needs no current-week lookup: we file
 * what Sleeper actually has.
 */
function msFetchMatchups(leagueId, season, state, league) {
  /* Only ask the live season for weeks that have started. A completed
   * season is asked for all 18 and can be cached forever, because a
   * finished box score does not change.
   *
   * These are two separate judgements and must not be collapsed into one
   * flag. "Known to be a past season" is what licenses caching forever;
   * "known to be the live season" is what licenses truncating the week
   * list. When the state lookup fails we know neither, and the safe
   * reading of an unknown is to cache briefly and ask for every week --
   * the opposite of what treating unknown as "not current" would do,
   * which would pin an in-progress season in the cache permanently. */
  var knownCurrent = !!(state && String(state.season) === String(season));
  var knownPast = !!(state && String(state.season) !== String(season));
  /* Never past the week this league's own season ends -- Sleeper answers
   * beyond it with a stale lineup and live NFL scoring. */
  var lastWeek = msLeagueLastWeek(league);
  if (knownCurrent) {
    var live = Number(state.week || state.display_week || 0);
    if (live > 0) lastWeek = Math.min(lastWeek, live);
  }
  var weeks = [];
  for (var w = 1; w <= lastWeek; w++) weeks.push(w);

  /* Cache only what the ledger reads. The raw response also carries
   * starters_points, points, custom_points and matchup_id; dropping them
   * roughly halves what goes into sessionStorage, and starters_points is
   * redundant anyway -- verified against the live API that summing
   * players_points over starters reproduces it exactly. */
  function distil(m) {
    if (!m || !m.length) return [];
    return m.map(function (x) {
      return { roster_id: x.roster_id, players: x.players || [],
               starters: x.starters || [],
               players_points: x.players_points || {} };
    });
  }

  return msMapLimit(weeks, MS_MAX_CONCURRENCY, function (w) {
    return msGetCached(MS_SLEEPER + '/league/' + leagueId + '/matchups/' + w, [],
                       { immutable: knownPast, distil: distil })
      .then(function (m) {
        return { season: String(season), week: w, leagueId: leagueId,
                 matchups: (m && m.length) ? m : [] };
      });
  }).then(function (rows) {
    return rows.filter(function (r) { return r.matchups.length > 0; });
  });
}

/* Sleeper exposes transactions per scoring week. Week 0 carries the
 * off-season, which in a dynasty league is where most trades live. */
function msFetchTransactions(leagueId, season, state) {
  var knownPast = !!(state && String(state.season) !== String(season));
  var weeks = [];
  for (var w = 0; w <= 18; w++) weeks.push(w);
  return msMapLimit(weeks, MS_MAX_CONCURRENCY, function (w) {
    return msGetCached(MS_SLEEPER + '/league/' + leagueId + '/transactions/' + w, [],
                       { immutable: knownPast });
  }).then(function (chunks) {
    var all = [];
    chunks.forEach(function (c) { (c || []).forEach(function (t) { all.push(t); }); });
    return all;
  });
}

/* ------------------------------------------------------- input assembly */

function msBuildInput(seasons) {
  /* Manager identity is the Sleeper user_id, not roster_id: roster ids are
   * reassigned between seasons, user ids are not. */
  var managers = {};
  var rosterToUser = {};   /* "leagueId:rosterId" -> userId */

  seasons.forEach(function (s) {
    var nameByUser = {};
    (s.users || []).forEach(function (u) {
      if (!u || !u.user_id) return;
      nameByUser[u.user_id] = u.display_name || u.username || u.user_id;
    });
    (s.rosters || []).forEach(function (r) {
      if (!r) return;
      var uid = r.owner_id || ('roster:' + s.league.league_id + ':' + r.roster_id);
      rosterToUser[s.league.league_id + ':' + r.roster_id] = uid;
      if (!managers[uid]) {
        managers[uid] = { id: uid, name: nameByUser[uid] || ('Team ' + r.roster_id) };
      } else if (nameByUser[uid]) {
        managers[uid].name = nameByUser[uid];
      }
    });
  });

  function userFor(leagueId, rosterId) {
    return rosterToUser[leagueId + ':' + rosterId] || null;
  }

  var txDates = [];
  seasons.forEach(function (s) {
    (s.draftPicks || []).forEach(function (dp) {
      var d = msMsToDate(dp.draft.start_time || dp.draft.last_picked);
      if (d) txDates.push(d);
    });
    (s.transactions || []).forEach(function (t) {
      var d = msMsToDate(t.status_updated || t.created);
      if (d) txDates.push(d);
    });
  });

  return Promise.resolve().then(function () {
    var drafts = [], trades = [], waivers = [];
    var evaluable = 0, tooRecent = 0, offBoard = 0;

    seasons.forEach(function (s) {
      var lid = s.league.league_id;

      (s.draftPicks || []).forEach(function (dp) {
        var d = dp.draft;
        var date = msMsToDate(d.start_time || d.last_picked);
        var kind = (d.settings && d.settings.rounds && (d.type || '')) || '';
        var label = (d.season || '') + ' ' +
          ((d.metadata && d.metadata.name) ? d.metadata.name :
            (String(d.settings && d.settings.rounds) === '1' ? 'draft' :
              (dp.picks.length > 80 ? 'startup draft' : 'rookie draft')));
        var picks = (dp.picks || []).map(function (p) {
          var md = p.metadata || {};
          var nm = ((md.first_name || '') + ' ' + (md.last_name || '')).trim();
          var uid = p.picked_by || userFor(lid, p.roster_id);
          var c = msCaptureForPlayer(p.player_id, date, nm);
          if (c.evaluable) evaluable++; else if (c.vAt != null) tooRecent++; else offBoard++;
          return {
            managerId: uid, slot: Number(p.pick_no || 0),
            round: Number(p.round || 0), playerId: String(p.player_id || ''),
            name: c.name, pos: c.pos || md.position || '',
            evaluable: c.evaluable, capture: c.capture,
            vAt: c.vAt, vAtDate: c.vAtDate, peak: c.peak, peakDate: c.peakDate,
            reason: c.reason, date: date
          };
        }).filter(function (p) { return p.managerId && p.slot > 0; });
        if (picks.length) {
          drafts.push({ id: d.draft_id || d.id, season: d.season,
                        label: label, kind: kind, date: date, picks: picks });
        }
      });

      (s.transactions || []).forEach(function (t) {
        if (!t || t.status !== 'complete') return;
        var date = msMsToDate(t.status_updated || t.created);
        var type = t.type;

        if (type === 'trade') {
          var bySide = {};
          function side(uid) {
            if (!uid) return null;
            if (!bySide[uid]) bySide[uid] = { managerId: uid, received: [], given: [] };
            return bySide[uid];
          }
          Object.keys(t.adds || {}).forEach(function (pid) {
            var uid = userFor(lid, t.adds[pid]);
            var sd = side(uid); if (!sd) return;
            var c = msCaptureForPlayer(pid, date, null);
            if (c.evaluable) evaluable++; else if (c.vAt != null) tooRecent++; else offBoard++;
            sd.received.push({ kind: 'player', playerId: String(pid),
                               label: c.name, pos: c.pos,
                               evaluable: c.evaluable, capture: c.capture,
                               vAt: c.vAt, peak: c.peak, reason: c.reason });
          });
          Object.keys(t.drops || {}).forEach(function (pid) {
            var uid = userFor(lid, t.drops[pid]);
            var sd = side(uid); if (!sd) return;
            var c = msCaptureForPlayer(pid, date, null);
            sd.given.push({ kind: 'player', playerId: String(pid),
                            label: c.name, pos: c.pos,
                            evaluable: c.evaluable, capture: c.capture,
                            vAt: c.vAt, peak: c.peak, reason: c.reason });
          });
          (t.draft_picks || []).forEach(function (dpk) {
            var to = userFor(lid, dpk.owner_id);
            var from = userFor(lid, dpk.previous_owner_id);
            var pc = msCaptureForPick(dpk.season, dpk.round, date);
            if (pc.evaluable) evaluable++; else tooRecent++;
            var asset = { kind: 'pick', label: pc.name,
                          evaluable: pc.evaluable, capture: pc.capture,
                          vAt: pc.vAt, peak: pc.peak, reason: pc.reason };
            var st = side(to); if (st) st.received.push(asset);
            var sf = side(from); if (sf) sf.given.push(asset);
          });
          /* FAAB moved in a trade. Not priceable in KTC points, but its
           * presence explains an unbalanced trade, so it must be carried
           * through rather than dropped. */
          var faabLegs = (t.waiver_budget || []).map(function (leg) {
            return {
              from: userFor(lid, leg.sender), to: userFor(lid, leg.receiver),
              amount: leg.amount
            };
          });
          var sides = Object.keys(bySide).map(function (k) { return bySide[k]; });
          if (sides.length >= 2) {
            /* season + leg locate the trade in the weekly ledger, which is
             * how the realized lens knows where to start counting. `leg` is
             * Sleeper's own scoring week for the transaction. */
            trades.push({ id: String(t.transaction_id || ''), date: date,
                          basis: date ? null : 'current', sides: sides,
                          faab: faabLegs, leagueId: lid,
                          season: String(s.league.season || ''),
                          week: Number(t.leg || 1) });
          }
        } else if (type === 'waiver' || type === 'free_agent') {
          var faab = null;
          if (t.settings && t.settings.waiver_bid != null) faab = t.settings.waiver_bid;
          Object.keys(t.adds || {}).forEach(function (pid) {
            var uid = userFor(lid, t.adds[pid]);
            if (!uid) return;
            var c = msCaptureForPlayer(pid, date, null);
            if (c.evaluable) evaluable++; else if (c.vAt != null) tooRecent++; else offBoard++;
            waivers.push({ managerId: uid, date: date, playerId: String(pid),
                           name: c.name, pos: c.pos,
                           evaluable: c.evaluable, capture: c.capture,
                           vAt: c.vAt, vAtDate: c.vAtDate, peak: c.peak,
                           peakDate: c.peakDate, reason: c.reason,
                           faab: faab, type: type });
          });
        }
      });
    });

    return {
      input: {
        managers: Object.keys(managers).map(function (k) { return managers[k]; }),
        drafts: drafts, trades: trades, waivers: waivers
      },
      coverage: { evaluable: evaluable, tooRecent: tooRecent, offBoard: offBoard },
      /* The realized lens resolves weekly matchup rosters to managers with
       * this, for the same reason the scoring does: roster ids are recycled
       * between seasons, user ids are not. */
      rosterToUser: rosterToUser
    };
  });
}

/* ---------------------------------------------------------------- render */

function msFmt(n, digits) {
  if (n == null || isNaN(n)) return '—';
  return Number(n).toFixed(digits == null ? 0 : digits);
}

function msSigned(n, digits) {
  if (n == null || isNaN(n)) return '—';
  var s = Number(n).toFixed(digits == null ? 0 : digits);
  return (Number(n) > 0 ? '+' : '') + s;
}

function msDeltaClass(n) {
  if (n == null || isNaN(n)) return '';
  return Number(n) > 0 ? 'ms-pos' : (Number(n) < 0 ? 'ms-neg' : '');
}

function msRenderBasisBanner(coverage) {
  var s = MSX.series || {};
  var dates = s.dates || [];
  var box = msEl('ms-basis');
  if (!box) return;
  var ev = coverage.evaluable || 0;
  var recent = coverage.tooRecent || 0;
  var off = coverage.offBoard || 0;

  var html;
  if (!dates.length) {
    html = '<strong>No dated value history available.</strong> This page ' +
      'needs <code>managerscore_series.json</code> to price anything, and it ' +
      'is missing. Nothing has been scored.';
  } else {
    html = '<strong>Point-in-time value capture.</strong> Every asset is ' +
      'priced at its KeepTradeCut value <em>on the date it changed hands</em>, ' +
      'then compared with the highest value it reached <em>afterwards</em>. ' +
      'The gap is what the manager captured.' +
      '<br><br>This is deliberately not "value then versus value today": a ' +
      'player\'s value decays as he ages, so judging a 2023 trade by today\'s ' +
      'board would punish the passage of time rather than the decision. It is ' +
      'also not "highest ever", which would reward buying a player who had ' +
      'already peaked. Measuring forward from each transaction isolates the ' +
      'thing that is actually skill: acquiring before a rise, and shedding ' +
      'before a fall.' +
      '<br><br><strong>The archive is real but sparse.</strong> ' + dates.length +
      ' dated boards from <code>' + msEsc(dates[0]) + '</code> to <code>' +
      msEsc(dates[dates.length - 1]) + '</code>, recovered from public web ' +
      'archive captures. Gaps of weeks to months exist, so a transaction is ' +
      'priced at the nearest recorded board <em>on or before</em> its date ' +
      '(never after), and a "peak" is the highest value we actually observed ' +
      '&mdash; the true peak may sit in a gap. Every row shows the dates used.' +
      '<br><br>' + ev + ' asset valuation(s) evaluated' +
      (recent ? ', <strong>' + recent + ' too recent to judge</strong> (nothing ' +
        'recorded after them yet &mdash; you cannot grade foresight on a trade ' +
        'made last week)' : '') +
      (off ? ', ' + off + ' never on the KTC board' : '') + '.';
  }
  box.className = 'callout callout-warn';
  box.style.display = 'block';
  box.innerHTML = html;
}

function msRenderSummary(result, coverage) {
  var m = result.meta;
  var s = MSX.series || {};
  var dates = s.dates || [];
  var kpis = [
    [String(result.managers.length), 'Managers scored'],
    [String(m.nPicksScored), 'Draft picks scored'],
    [String(m.nTradesScored) + ' / ' + String(m.nTrades), 'Trades scored'],
    [String(m.nWaivers), 'Waiver / FA adds scored'],
    [String(dates.length), 'Dated boards in archive'],
    [String(coverage.tooRecent || 0), 'Assets too recent to judge']
  ];
  msEl('ms-summary').innerHTML =
    '<div class="kpi-row">' + kpis.map(function (k) {
      return '<div class="kpi"><div class="num">' + msEsc(k[0]) + '</div>' +
             '<div class="label">' + msEsc(k[1]) + '</div></div>';
    }).join('') + '</div>' +
    '<div class="ms-sub">Components in play: ' +
    msEsc(m.liveComponents.join(', ') || 'none') +
    ' · weights ' + (Object.keys(result.weights).filter(function (c) {
      return result.weights[c] > 0;
    }).map(function (c) {
      return c + ' ' + (result.weights[c] * 100).toFixed(0) + '%';
    }).join(' / ') || '—') +
    ' · values are KeepTradeCut superflex consensus points</div>';
}

function msRenderTable(result) {
  var rows = result.managers.map(function (r) {
    return '<tr class="ms-row" data-mid="' + msEsc(r.id) + '">' +
      '<td class="rank">' + r.rank + '</td>' +
      '<td class="name">' + msEsc(r.name) + '</td>' +
      '<td class="score">' + msFmt(r.index, 1) + '</td>' +
      '<td class="' + msDeltaClass(r.draft.z) + '">' + msSigned(r.draft.z, 2) +
        '<span class="ms-n">' + r.draft.n + 'p</span></td>' +
      '<td class="' + msDeltaClass(r.trade.z) + '">' + msSigned(r.trade.z, 2) +
        '<span class="ms-n">' + r.trade.n + 't</span></td>' +
      '<td class="' + msDeltaClass(r.waiver.z) + '">' + msSigned(r.waiver.z, 2) +
        '<span class="ms-n">' + r.waiver.n + 'w</span></td>' +
      '<td class="' + msDeltaClass(r.draft.mean) + '">' + msSigned(r.draft.mean) + '</td>' +
      '<td class="' + msDeltaClass(r.trade.total) + '">' + msSigned(r.trade.total) + '</td>' +
      '<td class="ms-flags">' + msEsc(r.flags.join('; ') || '—') + '</td>' +
      '</tr>';
  }).join('');

  msEl('ms-table').innerHTML =
    '<table><thead><tr>' +
    '<th>#</th><th>Manager</th><th class="score">Manager Score</th>' +
    '<th>Draft z</th><th>Trade z</th><th>Waiver z</th>' +
    '<th>Draft surplus/pick</th><th>Trade net total</th><th>Notes</th>' +
    '</tr></thead><tbody>' + rows + '</tbody></table>' +
    '<p class="ms-sub">Click a manager to audit the individual transactions ' +
    'behind their score.</p>';

  Array.prototype.forEach.call(
    document.querySelectorAll('.ms-row'),
    function (tr) {
      tr.addEventListener('click', function () {
        msRenderAudit(tr.getAttribute('data-mid'));
      });
    }
  );
}

function msRenderAudit(managerId) {
  var result = MSX.result;
  if (!result) return;
  var mgr = null;
  result.managers.forEach(function (r) { if (r.id === managerId) mgr = r; });
  if (!mgr) return;

  var picks = result.audit.picks.filter(function (p) { return p.managerId === managerId; })
    .sort(function (a, b) { return b.surplus - a.surplus; });
  var waivers = result.audit.waivers.filter(function (w) { return w.managerId === managerId; })
    .sort(function (a, b) { return b.surplus - a.surplus; });
  var trades = result.audit.trades.filter(function (t) {
    return t.sides.some(function (s) { return s.managerId === managerId; });
  });

  var html = '<h3>' + msEsc(mgr.name) + ' — Manager Score ' +
    msFmt(mgr.index, 1) + ' (rank ' + mgr.rank + ' of ' +
    result.managers.length + ')</h3>';

  html += '<p class="ms-sub">Draft z ' + msSigned(mgr.draft.z, 2) +
    ' from ' + mgr.draft.n + ' picks · Trade z ' + msSigned(mgr.trade.z, 2) +
    ' from ' + mgr.trade.n + ' trades · Waiver z ' + msSigned(mgr.waiver.z, 2) +
    ' from ' + mgr.waiver.n + ' adds. ' +
    'Shrunk means: draft ' + msSigned(mgr.draft.shrunk) +
    ', trade ' + msSigned(mgr.trade.shrunk) +
    ', waiver ' + msSigned(mgr.waiver.shrunk) + '.</p>';

  html += '<h4>Draft picks (' + picks.length + ')</h4>';
  html += picks.length ? '<table><thead><tr><th>Draft</th><th>Slot</th>' +
    '<th>Player</th><th>Value at pick</th><th>Peak after</th><th>Captured</th>' +
    '<th>Slot par</th><th>Surplus</th></tr></thead><tbody>' +
    picks.map(function (p) {
      return '<tr><td>' + msEsc(p.draftLabel || p.season || '') + '</td>' +
        '<td>' + p.slot + '</td>' +
        '<td class="name">' + msEsc(p.name) + '</td>' +
        '<td>' + msFmt(p.vAt) + '<span class="ms-basis"> ' +
          msEsc(p.vAtDate || '') + '</span></td>' +
        '<td>' + msFmt(p.peak) + '<span class="ms-basis"> ' +
          msEsc(p.peakDate || '') + '</span></td>' +
        '<td>' + msFmt(p.capture) + '</td>' +
        '<td>' + msFmt(p.expected) + '</td>' +
        '<td class="' + msDeltaClass(p.surplus) + '">' + msSigned(p.surplus) + '</td></tr>';
    }).join('') + '</tbody></table>'
    : '<p class="ms-sub">No scored draft picks.</p>';

  html += '<h4>Trades (' + trades.length + ')</h4>';
  html += msRenderLensCompare(mgr, trades);
  html += trades.length ? trades.map(function (t) {
    var mine = null;
    t.sides.forEach(function (s) { if (s.managerId === managerId) mine = s; });
    if (!mine) return '';
    function assets(list) {
      return list.length ? list.map(function (a) {
        if (!a.evaluable) {
          return msEsc(a.label) + ' <span class="ms-basis">(' +
                 msEsc(a.reason || 'not priceable') + ')</span>';
        }
        return msEsc(a.label) + ' <span class="ms-basis">(' + msFmt(a.vAt) +
               ' &rarr; ' + msFmt(a.peak) + ', captured ' + msFmt(a.capture) +
               ')</span>';
      }).join(', ') : '—';
    }
    return '<div class="ms-trade' + (t.scored ? '' : ' ms-trade-unscored') + '">' +
      '<div class="ms-trade-head">' + msEsc(t.date || 'undated') +
      ' · market net <span class="' + msDeltaClass(mine.net) + '">' +
      msSigned(mine.net) + '</span>' +
      (t.scored ? '' : (t.unbalanced
        ? ' · <strong>not scored</strong> (sides did not balance — likely a' +
          ' parsing problem, imbalance ' + msFmt(t.imbalance) + ')'
        : ' · <strong>not scored</strong> (an asset could not be priced)')) +
      (t.faab && t.faab.length
        ? ' · includes ' + t.faab.map(function (l) { return '$' + msEsc(l.amount); })
            .join(' + ') + ' FAAB, which is not priced'
        : '') +
      '</div>' +
      '<div class="ms-trade-body"><span class="ms-got">got</span> ' +
      assets(mine.assets.received || []) + '<br>' +
      '<span class="ms-gave">gave</span> ' + assets(mine.assets.given || []) +
      '</div>' +
      msRenderRealizedTrade(t, managerId) +
      '</div>';
  }).join('') : '<p class="ms-sub">No trades on record.</p>';

  html += '<h4>Waiver / free-agent adds (' + waivers.length + ')</h4>';
  html += waivers.length ? '<table><thead><tr><th>Date</th><th>Player</th>' +
    '<th>Value at add</th><th>Peak after</th><th>Captured</th><th>FAAB</th>' +
    '</tr></thead><tbody>' +
    waivers.map(function (w) {
      return '<tr><td>' + msEsc(w.date || '—') + '</td>' +
        '<td class="name">' + msEsc(w.name) + '</td>' +
        '<td>' + msFmt(w.vAt) + '</td>' +
        '<td>' + msFmt(w.peak) + '<span class="ms-basis"> ' +
          msEsc(w.peakDate || '') + '</span></td>' +
        '<td class="' + msDeltaClass(w.surplus) + '">' + msSigned(w.surplus) + '</td>' +
        '<td>' + (w.faab == null ? '—' : msEsc(w.faab)) + '</td></tr>';
    }).join('') + '</tbody></table>'
    : '<p class="ms-sub">No waiver or free-agent adds on record.</p>';

  var box = msEl('ms-audit');
  box.style.display = 'block';
  box.innerHTML = html;
  box.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

/* ------------------------------------------- realized production render */

/* The two lenses answer different questions, so they are reported as two
 * numbers and never reconciled into one. Where they point opposite ways
 * that is said out loud: a manager who traded badly by the market and well
 * by the scoreboard has learned something real about their own league, and
 * averaging the two would delete exactly that signal. */
function msRenderLensCompare(mgr, trades) {
  var rz = mgr.realized;
  if (!rz || !rz.n) return '';
  var market = mgr.trade.total;
  var html = '<p class="ms-sub" style="margin-bottom:8px">' +
    '<span class="ms-lens ms-lens-market">market</span>' +
    'net captured value ' + msSigned(market) + ' over ' + mgr.trade.n +
    ' scored trade' + (mgr.trade.n === 1 ? '' : 's') + ' · ' +
    '<span class="ms-lens ms-lens-real">realized</span>' +
    'net ' + msSigned(rz.netPar, 1) + ' PAR (' + msFmt(rz.pts, 1) +
    ' pts acquired) over ' + rz.n + ' measurable trade' +
    (rz.n === 1 ? '' : 's') + '.</p>';

  var disagree = (market > 0 && rz.netPar < 0) || (market < 0 && rz.netPar > 0);
  if (disagree) {
    html += '<div class="ms-disagree"><strong>The two lenses disagree here.</strong> ' +
      (market > 0
        ? 'By market value this manager acquired the better assets; by the ' +
          'scoreboard those assets produced less than what they gave up. ' +
          'That is the shape of buying talent that did not play, got hurt, ' +
          'or sat on the bench.'
        : 'By market value this manager gave up more than they got; by the ' +
          'scoreboard what they acquired outproduced it. That is the shape ' +
          'of buying win-now production cheaply — which is a real way to ' +
          'win a league, and a real way to lose value doing it.') +
      '</div>';
  }
  return html;
}

/* One line per acquired player: what he actually produced for the manager
 * who traded for him, and where that ranked at his position over exactly
 * the weeks the manager held him. The rank is what makes the total mean
 * something -- "147 points" is a number, "147 points, RB2 over those
 * weeks" is an argument. */
function msRealizedAssetLine(r) {
  var bits = [];
  /* Total rostered points lead, because that is the scored basis. */
  bits.push('<strong>' + msFmt(r.ptsTotal, 1) + ' pts</strong>');
  bits.push('over ' + r.weeksHeld + ' wk' + (r.weeksHeld === 1 ? '' : 's'));
  /* The lineup story: disclosed, never scored, and only when there is
   * one to tell. */
  if (r.benchPts > 0.05) {
    bits.push('<span class="ms-bench">' + msFmt(r.ptsStarted, 1) +
              ' of it started</span>');
  }
  if (r.parAvailable) {
    bits.push('PAR <span class="' + msDeltaClass(r.par) + '">' +
              msSigned(r.par, 1) + '</span>');
  }
  if (r.rank && r.rank.rank) {
    bits.push(msEsc(r.pos) + msEsc(String(r.rank.rank)) + ' of ' +
              r.rank.of + ' in that span');
  }
  var tail = '';
  if (r.departedWeek) {
    tail = ' <span class="ms-basis">(left the roster ' +
           msEsc(r.departedWeek.replace(':', ' wk')) + ')</span>';
  } else if (r.truncated) {
    tail = ' <span class="ms-basis">(still rostered — still accruing)</span>';
  }
  if (!r.arrived) {
    return '<li>' + msEsc(r.label) + ' <span class="ms-basis">— never appeared ' +
           'on the acquiring roster in the weekly records</span></li>';
  }
  return '<li>' + msEsc(r.label) + ' — ' + bits.join(' · ') + tail + '</li>';
}

function msRenderRealizedTrade(t, managerId) {
  var rz = t.realized;
  if (!rz) return '';
  if (!rz.measurable) {
    return '<div class="ms-realized"><div class="ms-realized-head">' +
      'Realized production</div><p class="ms-sub" style="margin:.3rem 0 0">' +
      'Nothing to measure yet — no weekly scoring records cover this trade.' +
      '</p></div>';
  }
  var mine = null, theirs = [];
  rz.sides.forEach(function (s) {
    if (s.managerId === managerId) mine = s; else theirs.push(s);
  });
  if (!mine) return '';

  var html = '<div class="ms-realized"><div class="ms-realized-head">' +
    'Realized production <span class="ms-basis">— points scored for the ' +
    'acquiring roster, in this league\'s own scoring</span></div>';

  html += '<div class="ms-realized-net">You received <strong>' +
    msFmt(mine.ptsTotal, 1) + '</strong> pts of production';
  if (mine.parAvailable) {
    html += ' (PAR <span class="' + msDeltaClass(mine.par) + '">' +
      msSigned(mine.par, 1) + '</span>, ' +
      msSigned(mine.parPerWeek, 2) + '/wk)';
  }
  var oppPts = 0;
  theirs.forEach(function (s) { oppPts += s.ptsTotal; });
  html += ' · they received <strong>' + msFmt(oppPts, 1) + '</strong> pts';
  html += ' · realized net <span class="' + msDeltaClass(mine.netPar) + '">' +
    msSigned(mine.netPar, 1) + ' PAR</span>';
  if (rz.ongoing) html += ' <span class="ms-basis">(ongoing)</span>';
  html += '</div>';
  if (mine.benchPts > 0.05) {
    html += '<p class="ms-sub" style="margin:.3rem 0 0">' +
      msFmt(mine.ptsStarted, 1) + ' of those points were actually in your ' +
      'lineup; ' + msFmt(mine.benchPts, 1) + ' were produced on your bench. ' +
      'Both are credited — this measures the acquisition, not the lineup ' +
      'card — but only the started points won you anything.</p>';
  }

  if (mine.assets.length) {
    html += '<ul class="ms-realized-list">' +
      mine.assets.map(msRealizedAssetLine).join('') + '</ul>';
  }
  theirs.forEach(function (s) {
    if (!s.assets.length) return;
    html += '<div class="ms-basis" style="margin-top:.35rem">Other side received:</div>' +
      '<ul class="ms-realized-list ms-realized-other">' +
      s.assets.map(msRealizedAssetLine).join('') + '</ul>';
  });
  if (rz.partial) {
    html += '<p class="ms-sub" style="margin:.35rem 0 0">Draft picks in this ' +
      'trade carry no realized figure — Sleeper does not say which selection ' +
      'came from which traded slot, so attributing a drafted player back to ' +
      'the pick that bought him would be a guess. The market lens above does ' +
      'price those picks.</p>';
  }
  return html + '</div>';
}

function msRenderDrafts(result) {
  var skipped = result.drafts.filter(function (d) { return !d.scored; });
  if (!skipped.length) { msEl('ms-drafts').style.display = 'none'; return; }
  var box = msEl('ms-drafts');
  box.style.display = 'block';
  box.className = 'callout';
  box.innerHTML = '<strong>' + skipped.length + ' draft(s) not scored.</strong> ' +
    'A slot-cost curve needs at least ' + MS_MIN_PICKS_PER_DRAFT +
    ' priced picks to mean anything: ' +
    skipped.map(function (d) {
      return msEsc((d.label || d.season || d.id) + ' — ' + d.nValued +
                   ' priced pick(s)');
    }).join('; ') + '.';
}

/* ------------------------------------------------------------------ flow */

function msRun(leagueId, includeHistory) {
  msStatus('Reading league from Sleeper…');
  msEl('ms-results').style.display = 'none';
  msEl('ms-audit').style.display = 'none';

  return msLoadValues().then(function (values) {
    if (!values || !values.available) {
      msStatus('<strong>KeepTradeCut values are unavailable.</strong> ' +
        '<code>managerscore_values.json</code> was not built (or contains no ' +
        'players), so transactions cannot be priced and no score can be ' +
        'computed. It is written by the site build from ' +
        '<code>data/consensus/ktc_latest.json</code> plus the dynastyprocess ' +
        'id crosswalk.' + (MSX.loadError ?
          ' <br><span class="ms-basis">' + msEsc(MSX.loadError) + '</span>' : ''),
        'warn');
      return null;
    }
    return msFetchLeagueData(leagueId, includeHistory)
      .then(function (seasons) {
        msStatus('Pricing ' + seasons.length + ' season(s) of transactions…');
        return msBuildInput(seasons).then(function (built) {
          built.seasons = seasons;
          return built;
        });
      })
      .then(function (built) {
        var result = msScoreLeague(built.input, {});
        MSX.result = result;
        if (!result.managers.length) {
          msStatus('<strong>No managers found.</strong> Sleeper returned no ' +
                   'rosters for that league id.', 'warn');
          return null;
        }
        /* Second lens. Built from the weekly matchup records already
         * fetched alongside the transactions, and attached to the same
         * audit rows so the page can show market and realized side by
         * side. It never touches the index computed above. */
        var weekRows = [];
        (built.seasons || []).forEach(function (s) {
          (s.matchupWeeks || []).forEach(function (w) { weekRows.push(w); });
        });
        MSX.ledger = msBuildLedger(weekRows, built.rosterToUser);
        MSX.posOf = msPosLookup((MSX.values && MSX.values.by_sleeper) || {});
        msAttachRealized(result, built.input, MSX.ledger, MSX.posOf, {});
        msStatus('');
        msRenderBasisBanner(built.coverage);
        msRenderSummary(result, built.coverage);
        msRenderDrafts(result);
        msRenderTable(result);
        msEl('ms-results').style.display = 'block';
        var names = MSX.leagueChain.map(function (l) {
          return (l.name || l.league_id) + ' (' + (l.season || '?') + ')';
        }).join(' ← ');
        msEl('ms-league-label').innerHTML = msEsc(names);
        /* Optional cross-league corpus hook. Defined by corpus_submit_js.py
         * when the build has a corpus backend URL; absent otherwise, in
         * which case this page behaves exactly as it always has. Scoring
         * above is complete and unaffected either way. */
        if (typeof csOnScored === 'function') {
          try { csOnScored(leagueId, result, MSX.leagueChain); }
          catch (e) { /* indexing must never break the score on screen */ }
        }
        return result;
      })
      .catch(function (e) {
        msStatus('<strong>Could not score that league.</strong> ' +
                 msEsc(String(e && e.message || e)), 'warn');
        return null;
      });
  });
}

function msFindLeagues(username) {
  msStatus('Looking up ' + msEsc(username) + '…');
  return msGetJSONSoft(MS_SLEEPER + '/user/' + encodeURIComponent(username), null)
    .then(function (u) {
      if (!u || !u.user_id) throw new Error('No Sleeper user called "' + username + '".');
      var year = new Date().getFullYear();
      return msGetJSONSoft(
        MS_SLEEPER + '/user/' + u.user_id + '/leagues/nfl/' + year, []
      ).then(function (ls) {
        if (ls && ls.length) return ls;
        return msGetJSONSoft(
          MS_SLEEPER + '/user/' + u.user_id + '/leagues/nfl/' + (year - 1), []);
      });
    })
    .then(function (leagues) {
      if (!leagues || !leagues.length) {
        msStatus('That user has no NFL leagues Sleeper will show us.', 'warn');
        return;
      }
      msStatus('');
      msEl('ms-league-list').innerHTML =
        '<h3>Pick a league</h3>' + leagues.map(function (l) {
          return '<button class="btn ms-league-btn" data-lid="' +
            msEsc(l.league_id) + '">' + msEsc(l.name || l.league_id) +
            ' <span class="ms-n">' + msEsc(l.season || '') + ' · ' +
            msEsc(l.total_rosters || '?') + ' teams</span></button>';
        }).join('');
      Array.prototype.forEach.call(
        document.querySelectorAll('.ms-league-btn'),
        function (b) {
          b.addEventListener('click', function () {
            msRun(b.getAttribute('data-lid'), msEl('ms-history').checked);
          });
        }
      );
    })
    .catch(function (e) {
      msStatus(msEsc(String(e && e.message || e)), 'warn');
    });
}

function msInit() {
  var byUser = msEl('ms-load-user');
  if (byUser) {
    byUser.addEventListener('click', function () {
      var v = (msEl('ms-username').value || '').trim();
      if (v) msFindLeagues(v);
    });
  }
  var byId = msEl('ms-load-league');
  if (byId) {
    byId.addEventListener('click', function () {
      var v = (msEl('ms-leagueid').value || '').trim();
      if (v) msRun(v, msEl('ms-history').checked);
    });
  }
  msLoadValues().then(function (v) {
    var note = msEl('ms-artifact-note');
    if (!note) return;
    if (!v || !v.available) {
      note.style.display = 'block';
      note.className = 'callout callout-warn';
      note.innerHTML = '<strong>Value artifact missing.</strong> This page ' +
        'needs <code>managerscore_values.json</code> next to it. Until the ' +
        'site build writes one, league lookups will report the problem ' +
        'rather than score anything.';
      return;
    }
    var h = v.history || {};
    note.style.display = 'block';
    note.className = 'callout';
    note.innerHTML = '<strong>Value source.</strong> KeepTradeCut superflex ' +
      'consensus captured <code>' + msEsc((v.ktc && v.ktc.captured_at) || '?') +
      '</code> · ' + msEsc((v.ktc && v.ktc.n_players) || 0) + ' players, ' +
      'board floor ' + msEsc((v.ktc && v.ktc.floor) || '?') + ' · ' +
      msEsc((v.crosswalk && v.crosswalk.n_mapped) || 0) +
      ' mapped to Sleeper ids · dated value history: ' +
      (h.count ? '<strong>' + h.count + ' day(s)</strong> from <code>' +
        msEsc(h.earliest) + '</code>' : '<strong>none yet</strong>') + '.';
  });
}

if (typeof document !== 'undefined' && document.addEventListener) {
  document.addEventListener('DOMContentLoaded', msInit);
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports.msHistoryDates = msHistoryDates;
  module.exports.msMsToDate = msMsToDate;
  module.exports.msCaptureForPlayer = msCaptureForPlayer;
  module.exports.msCaptureForPick = msCaptureForPick;
  module.exports.msBuildInput = msBuildInput;
  module.exports.msRenderAudit = msRenderAudit;
  module.exports.msRenderTable = msRenderTable;
  module.exports.msRenderBasisBanner = msRenderBasisBanner;
  module.exports.msRenderSummary = msRenderSummary;
  module.exports.msRun = msRun;
  module.exports.msInit = msInit;
  module.exports.MSX = MSX;
  module.exports.msCacheClear = msCacheClear;
  module.exports.msMapLimit = msMapLimit;
  module.exports.msGetCached = msGetCached;
}
"""
