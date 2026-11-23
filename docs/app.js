/* Sidan räknar ingenting. Allt kommer färdigt ur data/summary.json. */

(function () {
  "use strict";

  var SVG = "http://www.w3.org/2000/svg";

  var kr = new Intl.NumberFormat("sv-SE", {
    style: "currency",
    currency: "SEK",
    maximumFractionDigits: 0
  });

  function pct(value) {
    if (value === null || value === undefined) return "–";
    return (value > 0 ? "+" : "") + value.toFixed(2).replace(".", ",") + " %";
  }

  function enheter(value) {
    if (value === null || value === undefined) return "–";
    return (value > 0 ? "+" : "") + value.toFixed(2).replace(".", ",");
  }

  function riktning(value) {
    if (value === null || value === undefined || value === 0) return "";
    return value > 0 ? "upp" : "ned";
  }

  function satt(id, text, klass) {
    var el = document.getElementById(id);
    if (!el) return;
    el.textContent = text;
    el.className = klass === undefined ? el.className : klass;
  }

  function element(tag, klass, text) {
    var el = document.createElement(tag);
    if (klass) el.className = klass;
    if (text !== undefined && text !== null) el.textContent = text;
    return el;
  }

  function datum(iso) {
    if (!iso) return "";
    return String(iso).slice(0, 10);
  }

  /* ------------------------------------------------------- rendering */

  function visaResultat(data) {
    var perf = data.performance || {};

    satt("portfolj-varde", kr.format(data.total_value || 0), "belopp");
    satt("index-varde", kr.format((data.benchmark && data.benchmark.value_sek) || 0), "belopp");

    satt("portfolj-avkastning", pct(perf.total_return_pct), "forandring " + riktning(perf.total_return_pct));
    satt("index-avkastning", pct(perf.index_return_pct), "forandring " + riktning(perf.index_return_pct));

    var overskott = perf.excess_pct;
    var text;
    if (overskott === null || overskott === undefined) {
      text = "Ingen jämförelse än.";
    } else if (overskott > 0) {
      text = "Portföljen ligger " + enheter(overskott) + " procentenheter före OMXS30.";
    } else if (overskott < 0) {
      text = "Portföljen ligger " + Math.abs(overskott).toFixed(2).replace(".", ",") +
             " procentenheter efter OMXS30.";
    } else {
      text = "Portföljen ligger jämnt med OMXS30.";
    }
    satt("overskott", text, "overskott " + riktning(overskott));

    satt("idag-portfolj", pct(perf.today_pct), riktning(perf.today_pct));
    satt("idag-index", pct(perf.index_today_pct), riktning(perf.index_today_pct));
    satt("idag-skillnad", enheter(perf.today_excess_pct), riktning(perf.today_excess_pct));
    satt("kassa", kr.format(data.cash || 0), "");
  }

  function visaPositioner(data) {
    var box = document.getElementById("positioner");
    box.innerHTML = "";

    var positioner = data.positions || [];
    if (!positioner.length) {
      box.appendChild(element("p", "tom", "Inga öppna positioner. Hela kapitalet ligger i kassa."));
      return;
    }

    positioner.forEach(function (p) {
      var post = element("div", "post");

      var topp = element("div", "post-topp");
      var vanster = element("div");
      vanster.appendChild(element("span", "namn", p.name || p.ticker));
      vanster.appendChild(document.createTextNode(" "));
      vanster.appendChild(element("span", "ticker", p.ticker));
      topp.appendChild(vanster);

      var pnl = p.unrealized_pnl;
      topp.appendChild(element(
        "span",
        "resultat-tal " + riktning(pnl),
        (pnl === undefined || pnl === null ? "–" : kr.format(pnl)) +
          (p.unrealized_pct === null || p.unrealized_pct === undefined
            ? "" : " (" + pct(p.unrealized_pct) + ")")
      ));
      post.appendChild(topp);

      var rader = [
        p.shares + " st à " + (p.avg_price || 0).toFixed(2).replace(".", ",") + " kr",
        "nu " + (p.last_price || 0).toFixed(2).replace(".", ",") + " kr",
        "värde " + kr.format(p.market_value || 0),
        (p.weight_pct === null || p.weight_pct === undefined
          ? "" : p.weight_pct.toFixed(1).replace(".", ",") + " % av portföljen"),
        "stopp " + (p.stop_loss || 0).toFixed(2).replace(".", ",") +
          " / mål " + (p.take_profit || 0).toFixed(2).replace(".", ",")
      ].filter(Boolean);
      post.appendChild(element("p", "detaljer", rader.join(" · ")));

      if (p.rationale) {
        post.appendChild(element("p", "motivering", p.rationale));
      }
      box.appendChild(post);
    });
  }

  function visaAffarer(data) {
    var box = document.getElementById("affarer");
    box.innerHTML = "";

    var affarer = data.recent_closed || [];
    if (!affarer.length) {
      box.appendChild(element("p", "tom", "Ingen affär är stängd än."));
      return;
    }

    var orsaker = {
      stop_loss: "stoppad",
      take_profit: "nådde målet",
      decision: "beslut"
    };

    affarer.forEach(function (t) {
      var post = element("div", "post");

      var topp = element("div", "post-topp");
      var vanster = element("div");
      vanster.appendChild(element("span", "namn", t.ticker));
      vanster.appendChild(document.createTextNode(" "));
      vanster.appendChild(element("span", "marke", orsaker[t.reason] || t.reason));
      topp.appendChild(vanster);

      topp.appendChild(element(
        "span",
        "resultat-tal " + riktning(t.realized_pnl),
        t.realized_pnl === undefined || t.realized_pnl === null
          ? "–" : kr.format(t.realized_pnl)
      ));
      post.appendChild(topp);

      post.appendChild(element("p", "detaljer", [
        datum(t.timestamp),
        t.shares + " st à " + (t.price || 0).toFixed(2).replace(".", ",") + " kr",
        "courtage " + (t.commission || 0).toFixed(2).replace(".", ",") + " kr"
      ].join(" · ")));

      if (t.rationale) {
        post.appendChild(element("p", "motivering", t.rationale));
      }
      box.appendChild(post);
    });
  }

  function visaLagesbild(data) {
    var beslut = data.last_decision;
    var lage = document.getElementById("lagesbild");
    var avvisade = document.getElementById("avvisade");
    avvisade.innerHTML = "";

    if (!beslut) {
      lage.textContent = "Ingen bedömning sparad från senaste körningen.";
      return;
    }

    lage.textContent = beslut.market_view || "Ingen kommentar.";

    var rejected = beslut.rejected || [];
    if (!rejected.length) return;

    var rubrik = element("p", "detaljer", "Förslag som koden avvisade:");
    avvisade.appendChild(rubrik);

    rejected.forEach(function (r) {
      var p = r.proposal || {};
      avvisade.appendChild(element(
        "p",
        "detaljer",
        "· " + (p.action || "?") + " " + (p.ticker || "?") + " – " + (r.reason || "?")
      ));
    });
  }

  /* ----------------------------------------------------------- grafen */

  function ritaGraf(serie) {
    var svg = document.getElementById("graf");
    var not = document.getElementById("graf-not");
    svg.innerHTML = "";

    if (!serie || serie.length < 2) {
      not.textContent = "Grafen ritas när experimentet har minst två dagar bakom sig.";
      return;
    }
    not.textContent = "";

    var B = 800, H = 320, pad = 8;
    var varden = [];
    serie.forEach(function (p) { varden.push(p.portfolio, p.index); });

    var min = Math.min.apply(null, varden);
    var max = Math.max.apply(null, varden);
    if (max === min) { max = min + 1; }
    var marginal = (max - min) * 0.08;
    min -= marginal;
    max += marginal;

    function x(i) { return pad + (i / (serie.length - 1)) * (B - pad * 2); }
    function y(v) { return H - pad - ((v - min) / (max - min)) * (H - pad * 2); }

    function linje(nyckel, farg, bredd, streckad) {
      var punkter = serie.map(function (p, i) {
        return x(i).toFixed(2) + "," + y(p[nyckel]).toFixed(2);
      }).join(" ");

      var el = document.createElementNS(SVG, "polyline");
      el.setAttribute("points", punkter);
      el.setAttribute("fill", "none");
      el.setAttribute("stroke", farg);
      el.setAttribute("stroke-width", bredd);
      el.setAttribute("stroke-linejoin", "round");
      el.setAttribute("stroke-linecap", "round");
      el.setAttribute("vector-effect", "non-scaling-stroke");
      if (streckad) el.setAttribute("stroke-dasharray", "5 4");
      svg.appendChild(el);
    }

    var stil = getComputedStyle(document.documentElement);
    linje("index", stil.getPropertyValue("--index").trim() || "#999", 1.5, true);
    linje("portfolio", stil.getPropertyValue("--portfolj").trim() || "#2f5fd0", 2.5, false);

    svg.setAttribute("aria-label",
      "Portföljens värde jämfört med OMXS30 från " + serie[0].date +
      " till " + serie[serie.length - 1].date);
  }

  /* ------------------------------------------------------------ start */

  function visaFel(meddelande) {
    var status = document.getElementById("status");
    status.textContent = meddelande;
  }

  fetch("data/summary.json", { cache: "no-store" })
    .then(function (svar) {
      if (!svar.ok) throw new Error("status " + svar.status);
      return svar.json();
    })
    .then(function (data) {
      if (data.disclaimer) {
        document.getElementById("disclaimer").textContent = data.disclaimer;
      }

      visaResultat(data);
      visaPositioner(data);
      visaAffarer(data);
      visaLagesbild(data);
      ritaGraf(data.series);

      var uppdaterad = document.getElementById("uppdaterad");
      uppdaterad.textContent = data.generated_at
        ? "Senast uppdaterad " + data.generated_at.replace("T", " ").slice(0, 16) +
          (data.slot ? " (" + data.slot + ")" : "")
        : "";

      document.getElementById("status").hidden = true;
      document.getElementById("innehall").hidden = false;
    })
    .catch(function (fel) {
      visaFel(
        "Ingen data ännu. Experimentet har inte kört sin första runda, " +
        "eller så gick den inte att hämta (" + fel.message + ")."
      );
    });
})();
