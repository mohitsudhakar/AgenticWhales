/* Discipline Tax share card — hand-rolled <canvas>, no html2canvas dependency.
 *
 * Design laws baked in:
 *  - Every number drawn here was computed SERVER-SIDE (the report payload);
 *    this file only draws — it never derives a dollar.
 *  - Anonymized by default: score, period, top leak KIND, benchmark band.
 *    The $ figure renders only behind an explicit toggle. Never symbols/sizes.
 *  - The benchmark band renders ONLY when cohort === 'real' (measured data).
 *  - Past-tense footer disclaimer on every card.
 */

const CARD_W = 1200, CARD_H = 630;

function _latestQuarter(report) {
  const q = report.quarterly || [];
  return q.length ? q[q.length - 1] : null;
}

function renderShareCard(report, opts) {
  opts = opts || {};
  const showDollars = !!opts.showDollars;
  const quarter = _latestQuarter(report);
  const period = opts.period || (quarter ? quarter.quarter : 'All time');
  const tax = quarter ? quarter.quantified_leak : report.total_quantified_leak;
  const topLeak = (quarter && quarter.top_leak_name) ||
    ((report.leaks || [])[0] ? report.leaks[0].name.split('(')[0].trim() : null);

  const c = document.createElement('canvas');
  c.width = CARD_W; c.height = CARD_H;
  const x = c.getContext('2d');

  // backdrop
  x.fillStyle = '#FAF6EE'; x.fillRect(0, 0, CARD_W, CARD_H);
  x.strokeStyle = '#E7E0D2'; x.lineWidth = 2;
  x.strokeRect(24, 24, CARD_W - 48, CARD_H - 48);

  // header
  x.fillStyle = '#6B6657';
  x.font = '600 22px Inter, sans-serif';
  x.fillText('🐋 AgenticWhales · Discipline Coach', 64, 88);
  x.fillStyle = '#1A1A17';
  x.font = 'italic 64px Georgia, serif';
  x.fillText('My discipline tax — ' + period, 64, 180);

  // score ring
  const cx = 990, cy = 320, R = 110;
  const score = Math.max(0, Math.min(100, report.discipline_score || 0));
  x.lineWidth = 22; x.strokeStyle = '#E7E0D2';
  x.beginPath(); x.arc(cx, cy, R, 0, Math.PI * 2); x.stroke();
  x.strokeStyle = score >= 70 ? '#1F6F54' : score >= 40 ? '#B45309' : '#B3261E';
  x.beginPath();
  x.arc(cx, cy, R, -Math.PI / 2, -Math.PI / 2 + (Math.PI * 2 * score) / 100);
  x.stroke();
  x.fillStyle = x.strokeStyle;
  x.font = '700 84px Georgia, serif'; x.textAlign = 'center';
  x.fillText(String(score), cx, cy + 28);
  x.fillStyle = '#6B6657'; x.font = '500 22px Inter, sans-serif';
  x.fillText('discipline score', cx, cy + 70);
  x.textAlign = 'left';

  // body
  let y = 280;
  if (showDollars && typeof tax === 'number') {
    x.fillStyle = '#1F6F54'; x.font = '700 96px Georgia, serif';
    const sign = tax < 0 ? '-' : '';
    x.fillText(sign + '$' + Math.abs(Math.round(tax)).toLocaleString(), 64, y + 60);
    x.fillStyle = '#6B6657'; x.font = '500 26px Inter, sans-serif';
    x.fillText('measured behavioral leak across my own trades', 64, y + 110);
    y += 160;
  } else {
    x.fillStyle = '#6B6657'; x.font = '500 30px Inter, sans-serif';
    x.fillText('Measured from my own trade history — dollars stay private.', 64, y + 50);
    y += 100;
  }
  if (topLeak) {
    x.fillStyle = '#1A1A17'; x.font = '600 32px Inter, sans-serif';
    x.fillText('Top pattern: ' + topLeak, 64, y + 40);
    y += 60;
  }
  // benchmark band: ONLY a real, measured cohort earns a comparative claim
  const b = report.benchmark;
  if (b && b.cohort === 'real' && b.band) {
    x.fillStyle = '#1F6F54'; x.font = '600 28px Inter, sans-serif';
    x.fillText(b.band + ' of ' + b.n_cohort + ' AgenticWhales traders', 64, y + 40);
  }

  // footer disclaimer (always)
  x.fillStyle = '#6B6657'; x.font = '400 20px Inter, sans-serif';
  x.fillText('Historical attribution from my own trades · not investment advice · agenticwhales /coach',
             64, CARD_H - 64);
  return c;
}

function downloadCard(canvas, filename) {
  canvas.toBlob((blob) => {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = filename || 'discipline-tax.png';
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  }, 'image/png');
}

async function shareCard(canvas, filename) {
  // Web Share API Level 2 — feature-detected; falls back to download
  // (navigator.canShare({files}) is unsupported on desktop Firefox/older Safari).
  return new Promise((resolve) => {
    canvas.toBlob(async (blob) => {
      const file = new File([blob], filename || 'discipline-tax.png', { type: 'image/png' });
      if (navigator.canShare && navigator.canShare({ files: [file] })) {
        try {
          await navigator.share({ files: [file], title: 'My discipline tax' });
          resolve('shared'); return;
        } catch (e) { /* user cancelled -> fall through to download */ }
      }
      downloadCard(canvas, filename);
      resolve('downloaded');
    }, 'image/png');
  });
}
