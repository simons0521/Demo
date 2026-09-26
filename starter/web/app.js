/* 经营看板 + 问答 + 调试面板。
 *
 * 零构建、零外链：不用框架、不用打包、不引远端资源，折线图是手写 SVG。
 * 评审在断网环境里按 README 跑起来，这个文件就是全部前端逻辑。
 *
 * 两条贯穿全文的规矩：
 *
 * 1. **所有进 DOM 的数据都过 `esc()`。** 回答正文来自大模型，trace 里还有
 *    知识库原文，都不该被当成 HTML 解释。
 * 2. **数值口径与后端一致**（`kbqa/render.py`）：金额两位小数，`orders`/`qty`
 *    取整，`aov` 为空显示 `—`。后端返回的金额单位已经是**元**，前端不再除 100。
 */
(function () {
  "use strict";

  // ---------------------------------------------------------------- 通用

  function $(selector, root) {
    return (root || document).querySelector(selector);
  }

  function esc(value) {
    if (value === null || value === undefined) return "";
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  /** 回答正文只放行 `**加粗**` 一种标记。**先转义再替换**——模型输出里的
   *  尖括号在上一步已经变不成标签了，这一步只动 `**`。 */
  function answerHtml(text) {
    return esc(text).replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  }

  var MONEY = { net_revenue: true, refund_amount: true, aov: true };

  /** 两位小数。金额与检索分数**共用这一份实现**：调试面板里"命中的分数"
   *  和"被过滤掉的文档本来会得多少分"要摆在一起比大小，两列口径不一致就没法比。
   *  实现只留一处，免得将来改了一边忘了另一边。 */
  function fixed2(value) {
    return value === null || value === undefined ? "—" : Number(value).toFixed(2);
  }

  function money(value) {
    return fixed2(value);
  }

  function count(value) {
    return value === null || value === undefined ? "—" : String(Math.round(Number(value)));
  }

  function num(key, value) {
    return MONEY[key] ? money(value) : count(value);
  }

  /** JSON 字符串尽量排版；解析不了就原样显示（trace 里存的是字符串）。 */
  function pretty(text) {
    if (text === null || text === undefined) return "";
    if (typeof text !== "string") return JSON.stringify(text, null, 2);
    try {
      return JSON.stringify(JSON.parse(text), null, 2);
    } catch (err) {
      return text;
    }
  }

  function json(value) {
    return JSON.stringify(value, null, 2);
  }

  function ms(value) {
    return value === null || value === undefined ? "—" : String(value);
  }

  async function api(path, options) {
    var response = await fetch(path, options);
    var text = await response.text();
    var payload;
    try {
      payload = JSON.parse(text);
    } catch (err) {
      throw new Error("接口 " + path + " 没有返回 JSON（HTTP " + response.status + "）");
    }
    if (!response.ok) {
      throw new Error(payload.error || "接口 " + path + " 返回 HTTP " + response.status);
    }
    return payload;
  }

  function fill(node, html) {
    node.innerHTML = html;
  }

  function message(node, text, isError) {
    fill(node, '<p class="' + (isError ? "error" : "hint") + '">' + esc(text) + "</p>");
  }

  // ---------------------------------------------------------------- 路由

  var TABS = ["dashboard", "chat", "debug"];

  function route() {
    var hash = location.hash || "#/dashboard";
    var rest = hash.replace(/^#\/?/, "");
    var slash = rest.indexOf("/");
    var name = (slash === -1 ? rest : rest.slice(0, slash)) || "dashboard";
    var argument = slash === -1 ? "" : rest.slice(slash + 1);
    if (TABS.indexOf(name) === -1) name = "dashboard";

    TABS.forEach(function (tab) {
      $("#view-" + tab).hidden = tab !== name;
      var link = $('.tabs a[data-tab="' + tab + '"]');
      if (link) link.className = tab === name ? "active" : "";
    });

    if (name === "dashboard") loadDashboard();
    if (name === "chat") $("#chat-input").focus();
    if (name === "debug") {
      if (argument) {
        $("#debug-id").value = decodeURIComponent(argument);
        loadTrace(decodeURIComponent(argument));
      } else {
        loadRecentTraces();
      }
    }
  }

  // ---------------------------------------------------------------- 看板

  var SUMMARY_METRICS = [
    ["net_revenue", "净营业额", "元"],
    ["refund_amount", "退款金额", "元"],
    ["orders", "有效订单数", "单"],
    ["aov", "客单价", "元"],
    ["qty", "销量", "件"],
  ];

  var dashboardReady = false;
  var healthCache = null;

  /** `/api/health` 只问一次：顶栏要它、看板初始化也要它。 */
  async function loadHealth() {
    if (!healthCache) healthCache = await api("/api/health");
    return healthCache;
  }

  async function loadDashboard() {
    var hint = $("#dash-hint");
    if (!dashboardReady) {
      dashboardReady = true;
      try {
        // 日期默认取 `/api/health` 的 `data_period`：**不能前端写死区间**，
        // 评审换掉 `data/` 之后数据区间就变了。
        var health = await loadHealth();
        $("#dash-start").value = health.data_period.start;
        $("#dash-end").value = health.data_period.end;
        var stores = (await api("/api/stores")).stores;
        var select = $("#dash-store");
        stores.forEach(function (store) {
          var option = document.createElement("option");
          option.value = store.store_id;
          option.textContent = store.store_id + " " + (store.store_name || "");
          select.appendChild(option);
        });
      } catch (err) {
        message(hint, "初始化失败：" + err.message, true);
        return;
      }
    }
    message(hint, "数据区间 " + $("#dash-start").value + " ~ " + $("#dash-end").value);
    await Promise.all([loadSummary(), loadTrend(), loadTopProducts(), loadQuality()]);
  }

  function dashQuery(extra) {
    var params = new URLSearchParams({
      start: $("#dash-start").value,
      end: $("#dash-end").value,
    });
    var store = $("#dash-store").value;
    if (store) params.set("store_id", store);
    if (extra) Object.keys(extra).forEach(function (key) { params.set(key, extra[key]); });
    return params.toString();
  }

  async function loadSummary() {
    var node = $("#dash-summary");
    try {
      var data = await api("/api/metrics/summary?" + dashQuery());
      fill(
        node,
        SUMMARY_METRICS.map(function (item) {
          var key = item[0], label = item[1], unit = item[2];
          return (
            '<div class="card"><div class="label">' + esc(label) + "</div>" +
            '<div class="value">' + esc(num(key, data[key])) +
            '<span class="unit">' + esc(unit) + "</span></div></div>"
          );
        }).join("")
      );
    } catch (err) {
      message(node, "概览加载失败：" + err.message, true);
    }
  }

  /** 手写折线图。`days` 后端已经按天零填充过了，这里不再补点。 */
  function renderTrend(days) {
    if (!days.length) return '<p class="hint">这个区间里没有数据。</p>';

    var W = 720, H = 220;
    var pad = { left: 48, right: 14, top: 14, bottom: 26 };
    var innerW = W - pad.left - pad.right;
    var innerH = H - pad.top - pad.bottom;

    var values = days.map(function (day) { return Number(day.net_revenue) || 0; });
    var max = Math.max.apply(null, values);
    var min = Math.min.apply(null, values);
    if (max === min) max = min + 1; // 一条水平线也要画得出来，别除以 0

    function x(index) {
      return days.length === 1
        ? pad.left + innerW / 2
        : pad.left + (innerW * index) / (days.length - 1);
    }
    function y(value) {
      return pad.top + innerH - ((value - min) / (max - min)) * innerH;
    }

    var parts = [];
    parts.push('<svg class="trend" viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="每日净营业额折线图">');

    // 上下两条网格线 + 金额刻度
    [max, min].forEach(function (value) {
      parts.push('<line class="grid" x1="' + pad.left + '" x2="' + (W - pad.right) +
                 '" y1="' + y(value).toFixed(1) + '" y2="' + y(value).toFixed(1) + '"/>');
      parts.push('<text x="' + (pad.left - 6) + '" y="' + (y(value) + 3).toFixed(1) +
                 '" text-anchor="end">' + esc(money(value)) + "</text>");
    });

    parts.push('<line class="axis" x1="' + pad.left + '" x2="' + pad.left +
               '" y1="' + pad.top + '" y2="' + (pad.top + innerH) + '"/>');

    var points = days.map(function (day, index) {
      return x(index).toFixed(1) + "," + y(Number(day.net_revenue) || 0).toFixed(1);
    });
    parts.push('<polyline class="line" points="' + points.join(" ") + '"/>');

    days.forEach(function (day, index) {
      var value = Number(day.net_revenue) || 0;
      parts.push(
        '<circle class="dot" cx="' + x(index).toFixed(1) + '" cy="' + y(value).toFixed(1) + '" r="2.5">' +
        "<title>" + esc(day.date + " 净营业额 " + money(value) + " 元，订单 " +
                         count(day.orders) + " 单，客单价 " + money(day.aov) + " 元") + "</title></circle>"
      );
    });

    // 横轴只标首、中、尾三个日期：都标上会糊成一片。
    // 两端的标签改成靠边对齐——居中会被 viewBox 裁掉半截（最后一个日期
    // 会显示成 "2026-08"，看着像数据缺了一截）。
    [0, Math.floor((days.length - 1) / 2), days.length - 1]
      .filter(function (index, position, list) { return list.indexOf(index) === position; })
      .forEach(function (index) {
        var anchor = "middle";
        if (days.length > 1 && index === 0) anchor = "start";
        if (days.length > 1 && index === days.length - 1) anchor = "end";
        parts.push(
          '<text x="' + x(index).toFixed(1) + '" y="' + (H - 8) + '" text-anchor="' + anchor + '">' +
          esc(days[index].date) + "</text>"
        );
      });

    parts.push("</svg>");
    return parts.join("");
  }

  async function loadTrend() {
    var node = $("#dash-trend");
    try {
      var data = await api("/api/metrics/daily?" + dashQuery());
      fill(node, renderTrend(data.days || []));
    } catch (err) {
      message(node, "趋势加载失败：" + err.message, true);
    }
  }

  async function loadTopProducts() {
    var node = $("#dash-top");
    try {
      var data = await api("/api/metrics/top_products?" + dashQuery({ limit: 10 }));
      var products = data.products || [];
      if (!products.length) {
        message(node, "这个区间里没有商品数据。");
        return;
      }
      fill(
        node,
        "<table><thead><tr><th>#</th><th>商品</th><th>品类</th>" +
          '<th class="num">净营业额</th><th class="num">订单</th><th class="num">销量</th></tr></thead><tbody>' +
          products.map(function (item, index) {
            return (
              "<tr><td>" + (index + 1) + "</td>" +
              "<td>" + esc(item.product_name) + ' <span class="mono hint">' + esc(item.product_id) + "</span></td>" +
              "<td>" + esc(item.product_category) + "</td>" +
              '<td class="num">' + esc(money(item.net_revenue)) + "</td>" +
              '<td class="num">' + esc(count(item.orders)) + "</td>" +
              '<td class="num">' + esc(count(item.qty)) + "</td></tr>"
            );
          }).join("") +
          "</tbody></table>"
      );
    } catch (err) {
      message(node, "Top 商品加载失败：" + err.message, true);
    }
  }

  async function loadQuality() {
    var node = $("#dash-quality");
    try {
      var data = await api("/api/data_quality");
      var report = data.cleaning_report || {};
      var reasons = data.removal_reasons || [];

      // `kind: "note"` 的那条**不是**剔除原因——它是"金额是垃圾值而非空值"的注解，
      // 已经计在 `2_empty_amount` 里了。把它加起来，总数就和 kept 对不上。
      var removedRows = reasons.filter(function (item) { return item.kind !== "note"; });
      var notes = reasons.filter(function (item) { return item.kind === "note"; });
      var removedTotal = removedRows.reduce(function (sum, item) {
        return sum + Number(item.removed || 0);
      }, 0);

      var html =
        '<div class="cards">' +
        card("原始行数", count(report.raw_rows), "") +
        card("保留行数", count(report.kept_rows), "") +
        card("其中销售", count(report.kept_sales_rows), "") +
        card("其中退款", count(report.kept_refund_rows), "") +
        card("剔除行数", count(removedTotal), "") +
        "</div>";

      html += "<h3>剔除原因</h3><table><thead><tr><th>原因</th>" +
              '<th class="num">行数</th><th>说明</th></tr></thead><tbody>' +
              removedRows.map(function (item) {
                return "<tr><td>" + esc(item.label_cn) + "</td>" +
                       '<td class="num">' + esc(count(item.removed)) + "</td>" +
                       '<td class="hint mono">' + esc(item.key) + "</td></tr>";
              }).join("") +
              "</tbody></table>";

      if (notes.length) {
        html += "<h3>注解（不参与剔除）</h3>" +
                notes.map(function (item) {
                  return '<p class="hint">' + esc(item.label_cn) + "：" +
                         esc(count(item.removed)) + " 行</p>";
                }).join("");
      }

      var kept = Number(report.kept_rows || 0);
      var raw = Number(report.raw_rows || 0);
      if (raw) {
        html += '<p class="hint">剔除 ' + esc(count(removedTotal)) + " 行，" +
                "保留 " + esc(count(kept)) + " 行；" +
                "剔除合计与原始 - 保留" + (removedTotal === raw - kept ? "一致。" : "**对不上**，请核。") +
                "</p>";
      }

      var warnings = data.kb_warnings || [];
      html += "<h3>知识库加载提示</h3>";
      html += warnings.length
        ? "<ul>" + warnings.map(function (item) { return "<li>" + esc(item) + "</li>"; }).join("") + "</ul>"
        : '<p class="hint">没有提示。</p>';

      fill(node, html);
    } catch (err) {
      message(node, "数据质量加载失败：" + err.message, true);
    }
  }

  function card(label, value, unit) {
    return '<div class="card"><div class="label">' + esc(label) + "</div>" +
           '<div class="value">' + esc(value) +
           (unit ? '<span class="unit">' + esc(unit) + "</span>" : "") + "</div></div>";
  }

  // ---------------------------------------------------------------- 问答

  function newSessionId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID();
    }
    return "web-" + Date.now() + "-" + Math.random().toString(16).slice(2);
  }

  var sessionId = newSessionId();

  function appendTurn(role, html) {
    var turn = document.createElement("div");
    turn.className = "turn " + (role === "user" ? "q" : "a");
    var who = document.createElement("div");
    who.className = "who";
    who.textContent = role === "user" ? "我" : "助手";
    var body = document.createElement("div");
    body.className = "answer";
    body.innerHTML = html;
    turn.appendChild(who);
    turn.appendChild(body);
    $("#chat-log").appendChild(turn);
    turn.scrollIntoView({ block: "end" });
    return body;
  }

  function renderAnswer(payload) {
    var html = answerHtml(payload.answer) +
      '<span class="answer-type ' + esc(payload.answer_type) + '">' + esc(payload.answer_type) + "</span>";

    var citations = payload.citations || [];
    if (citations.length) {
      html += "<details><summary>引用 " + citations.length + " 条</summary>" +
        citations.map(function (item) {
          return '<p class="mono hint">' + esc(item.doc_id) + "</p>" +
                 "<blockquote>" + answerHtml(item.quote) + "</blockquote>";
        }).join("") + "</details>";
    }

    var evidence = payload.data_evidence || [];
    if (evidence.length) {
      html += "<details><summary>数据证据 " + evidence.length + " 条</summary>" +
        evidence.map(function (item) {
          var body = "<p><strong>" + esc(item.tool) + "</strong></p>" +
                     "<h3>参数</h3><pre>" + esc(json(item.params)) + "</pre>";
          if (item.sql) body += "<h3>SQL</h3><pre>" + esc(item.sql) + "</pre>";
          body += "<h3>结果</h3><pre>" + esc(pretty(item.result)) + "</pre>";
          return body;
        }).join("") + "</details>";
    }

    html += '<p><button type="button" class="link" data-trace="' + esc(payload.trace_id) +
            '">看这次 trace</button> ' +
            '<span class="mono hint">' + esc(payload.trace_id) + "</span></p>";
    return html;
  }

  async function sendQuestion(question) {
    appendTurn("user", esc(question));
    var pending = appendTurn("assistant", '<span class="hint">思考中…</span>');
    var status = $("#chat-status");
    // live 模式一次问答要几十秒（模型的思考 + 多轮工具调用），必须说清楚，
    // 不然用户会以为卡死了。
    message(status, "思考中…配了 Key 的 live 模式一次可能要 30–60 秒，请不要重复提交。");
    $("#chat-send").disabled = true;

    try {
      var payload = await api("/api/chat", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ session_id: sessionId, question: question }),
      });
      pending.innerHTML = renderAnswer(payload);
      message(status, "本次 session：" + sessionId);
    } catch (err) {
      pending.innerHTML = '<p class="error">' + esc("请求失败：" + err.message) + "</p>";
      message(status, "请求失败：" + err.message, true);
    } finally {
      $("#chat-send").disabled = false;
    }
  }

  // ---------------------------------------------------------------- 调试面板

  function stepClass(name) {
    if (name === "search") return "search";
    if (name === "tool") return "tool";
    return "";
  }

  function renderErrors(errors) {
    if (!errors || !errors.length) return "";
    return '<h3 class="error">错误 ' + errors.length + " 处</h3>" +
      errors.map(function (item) {
        return '<div class="errors"><p><strong>' + esc(item.where) + "</strong> " +
               '<span class="tag bad">' + esc(item.type) + "</span></p>" +
               "<p>" + esc(item.message) + "</p>" +
               "<pre>" + esc(item.traceback) + "</pre></div>";
      }).join("");
  }

  function kvTable(pairs) {
    return "<table><tbody>" +
      pairs.map(function (pair) {
        return '<tr><th style="width:150px">' + esc(pair[0]) + "</th><td>" +
               esc(pair[1] === null || pair[1] === undefined || pair[1] === "" ? "—" : pair[1]) +
               "</td></tr>";
      }).join("") + "</tbody></table>";
  }

  function renderSearch(detail) {
    var hits = detail.hits || [];
    var html = "<h3>检索</h3>";
    html += kvTable([
      ["改写后的检索查询", detail.query],
      ["切出的词", (detail.terms || []).join(" ")],
      ["覆盖率", detail.coverage],
      ["命中片段", hits.length + " / " + detail.hits_total + " 条（trace 里最多列 20 条）"],
    ]);

    var args = detail.args || {};
    html += "<h3>本次检索的入参</h3>" + kvTable([
      ["as_of", args.as_of], ["store_id", args.store_id], ["year", args.year],
      ["window", args.window ? args.window.join(" ~ ") : ""], ["numeric", args.numeric],
      ["historical", args.historical], ["top_k", args.top_k],
    ]);

    html += "<h3>命中片段</h3>";
    if (!hits.length) {
      html += '<p class="hint">一段都没命中。</p>';
    } else {
      html += "<table><thead><tr><th>文档</th><th>片段</th><th class=\"num\">分数</th>" +
              "<th>位置</th><th>正文</th></tr></thead><tbody>" +
        hits.map(function (hit) {
          var tags = [];
          if (hit.padded) tags.push('<span class="tag warn">补齐</span>');
          if (hit.dropped_instructions && hit.dropped_instructions.length) {
            tags.push('<span class="tag">剔除了 ' + hit.dropped_instructions.length + " 条指令</span>");
          }
          return "<tr>" +
            '<td class="mono">' + esc(hit.doc_id) + "</td>" +
            '<td class="mono">' + esc(hit.chunk_id) + " " + tags.join(" ") + "</td>" +
            '<td class="num">' + esc(fixed2(hit.score)) + "</td>" +
            '<td class="hint">' + esc(hit.heading) + '<br><span class="mono">' + esc(hit.kind) + "</span></td>" +
            '<td class="hint">' + esc(hit.text) +
            (hit.text_chars > (hit.text || "").length
              ? '<br><span class="mono">（原文 ' + esc(hit.text_chars) + " 字，这里截断显示）</span>"
              : "") + "</td></tr>";
        }).join("") + "</tbody></table>";
    }

    var filtered = detail.filtered || [];
    if (filtered.length) {
      html += "<h3>被元数据过滤掉的文档 " + filtered.length + " 篇</h3>" +
        "<table><thead><tr><th>文档</th><th>原因</th>" +
        '<th class="num">本来会得多少分</th><th class="num">本来会排第几</th></tr></thead><tbody>' +
        filtered.map(function (item) {
          return '<tr><td class="mono">' + esc(item.doc_id) + "</td>" +
            "<td>" + esc(item.reason) + "</td>" +
            '<td class="num">' + esc(fixed2(item.would_be_score)) + "</td>" +
            '<td class="num">' + esc(item.would_be_rank === undefined ? "—" : item.would_be_rank) + "</td></tr>";
        }).join("") + "</tbody></table>";
    }

    var dropped = detail.dropped || [];
    var counts = detail.dropped_counts || {};
    var countText = Object.keys(counts).map(function (reason) {
      return reason + " × " + counts[reason];
    }).join("；");
    html += "<h3>被规则挤掉的片段</h3><p class=\"hint\">共 " + esc(detail.dropped_total) +
            " 条（trace 里最多列 30 条）" + (countText ? "；" + esc(countText) : "") + "</p>";
    if (dropped.length) {
      html += "<table><thead><tr><th>文档</th><th>片段</th>" +
              '<th class="num">分数</th><th>原因</th><th>哪一轮</th></tr></thead><tbody>' +
        dropped.map(function (item) {
          return '<tr><td class="mono">' + esc(item.doc_id) + "</td>" +
            '<td class="mono">' + esc(item.chunk_id) + "</td>" +
            '<td class="num">' + esc(fixed2(item.score)) + "</td>" +
            "<td>" + esc(item.reason) + "</td>" +
            '<td class="hint">' + esc(item.phase) + "</td></tr>";
        }).join("") + "</tbody></table>";
    }
    return html;
  }

  function renderSteps(steps) {
    if (!steps || !steps.length) return '<p class="hint">没有步骤。</p>';
    // **按数组顺序渲染**，不给同名步骤建索引：一次问答里 `search` 可能出现
    // 好几次（模型每查一次就是一步），按名字建字典会静默丢掉前面那些。
    return '<ul class="steps">' +
      steps.map(function (step, index) {
        var header = '<div><span class="step-name ' + stepClass(step.step) + '">' +
          esc(step.step) + "</span> " +
          '<span class="step-time">at ' + esc(ms(step.at_ms)) + " ms" +
          (step.took_ms === null || step.took_ms === undefined ? "" : " · 耗时 " + esc(ms(step.took_ms)) + " ms") +
          " · 第 " + (index + 1) + " 步</span></div>";
        return "<li>" + header + "<div>" + renderStepBody(step) + "</div></li>";
      }).join("") + "</ul>";
  }

  function renderStepBody(step) {
    var detail = step.detail;
    if (step.step === "search") return renderSearch(detail || {});
    if (step.step === "plan") return renderPlan(detail || {});
    if (step.step === "tool") {
      return "<h3>工具调用</h3>" + kvTable([
        ["工具", (detail || {}).tool],
        ["参数", JSON.stringify((detail || {}).params)],
      ]);
    }
    if (step.step === "response" || step.step === "answer_mock" || step.step === "answer_live") {
      return kvTable([
        ["answer_type", (detail || {}).answer_type],
        ["notes", ((detail || {}).notes || []).join("；")],
      ]);
    }
    // 形状不认识的步骤一律降级成 JSON：trace 的结构将来会演进，
    // 面板得能活下来，而不是白屏。
    return '<details><summary>这一步的形状面板不认，按 JSON 看</summary><pre>' +
           esc(json(detail)) + "</pre></details>";
  }

  function renderPlan(detail) {
    var html = "<h3>规划结果</h3>";
    html += kvTable([
      ["改写后的检索查询", detail.search_query],
      ["原问句", detail.question],
      ["补全后的独立问句", detail.standalone_question],
      ["意图", detail.intent],
      ["要查数据 / 要查文档", (detail.needs_data ? "要" : "不要") + " / " + (detail.needs_docs ? "要" : "不要")],
      ["门店 / 商品", [detail.store_id, detail.product_id].filter(Boolean).join(" ")],
      ["时间", [detail.as_of, detail.window ? detail.window.join(" ~ ") : "", detail.year].filter(Boolean).join(" · ")],
    ]);
    html += '<details><summary>规划的全部字段</summary><pre>' + esc(json(detail)) + "</pre></details>";
    return html;
  }

  function renderLlmCalls(calls) {
    if (!calls || !calls.length) return '<p class="hint">这次没有任何模型调用。</p>';
    return calls.map(function (call, index) {
      var usage = call.usage || {};
      var html = "<details><summary>第 " + (index + 1) + " 次调用 · " + esc(call.model) +
        " · " + esc(call.finish_reason) + " · " + esc(ms(call.took_ms)) + " ms</summary>";
      html += "<p class=\"hint\">endpoint " + esc(call.endpoint) + " · HTTP " + esc(call.status) +
        " · 消息 " + esc(call.messages) + " 条 · 工具 " + esc(call.tools) + " 个 · 正文 " +
        esc(call.content_chars) + " 字 · token " +
        esc((usage.prompt_tokens || 0) + " + " + (usage.completion_tokens || 0) + " = " + (usage.total_tokens || 0)) +
        " · 工具调用 " + esc((call.tool_calls || []).join("、") || "无") + "</p>";

      html += "<h3>最终提示词</h3><pre>" + esc(pretty(call.prompt)) + "</pre>";
      html += "<h3>模型原始输出</h3>";
      html += call.raw_content
        ? "<pre>" + esc(call.raw_content) + "</pre>"
        : '<p class="hint">这一轮没有正文（模型选择了调用工具）。</p>';
      // 思考过程只折在调试区，**不进答案区**。
      if (call.raw_reasoning) {
        html += "<h3>思考过程（raw_reasoning，只在这里看）</h3><pre>" + esc(call.raw_reasoning) + "</pre>";
      }
      return html + "</details>";
    }).join("");
  }

  function renderTrace(trace) {
    var html = kvTable([
      ["trace_id", trace.trace_id],
      ["session_id", trace.session_id],
      ["问题", trace.question],
      ["开始时间", trace.started_at],
      ["总耗时", ms(trace.total_ms) + " ms"],
    ]);
    html += renderErrors(trace.errors);
    html += "<h3>步骤时间线（按发生顺序）</h3>" + renderSteps(trace.steps);
    html += "<h3>最终提示词与模型原始输出</h3>" + renderLlmCalls(trace.llm_calls);
    return html;
  }

  async function loadTrace(traceId) {
    var node = $("#debug-body");
    if (!traceId) {
      message(node, "先填一个 trace_id，或者点「最近几次问答」挑一条。");
      return;
    }
    message(node, "加载中…");
    try {
      fill(node, renderTrace(await api("/api/trace/" + encodeURIComponent(traceId))));
    } catch (err) {
      message(node, "加载失败：" + err.message, true);
    }
  }

  async function loadRecentTraces() {
    var node = $("#debug-body");
    message(node, "加载中…");
    try {
      var traces = (await api("/api/traces?limit=20")).traces || [];
      if (!traces.length) {
        message(node, "还没有任何一次问答。先去「问答」里问一句。");
        return;
      }
      fill(node, "<h3>最近 " + traces.length + " 次问答</h3>" +
        "<table><thead><tr><th>trace_id</th><th>问题</th><th>时间</th>" +
        '<th class="num">总耗时</th><th class="num">步骤</th><th class="num">模型调用</th>' +
        '<th class="num">错误</th></tr></thead><tbody>' +
        traces.map(function (item) {
          return "<tr>" +
            '<td><a href="#/debug/' + encodeURIComponent(item.trace_id) + '">' + esc(item.trace_id) + "</a></td>" +
            "<td>" + esc(item.question) + "</td>" +
            '<td class="hint">' + esc(item.started_at) + "</td>" +
            '<td class="num">' + esc(ms(item.total_ms)) + "</td>" +
            '<td class="num">' + esc(item.step_count) + "</td>" +
            '<td class="num">' + esc(item.llm_call_count) + "</td>" +
            '<td class="num">' + (item.error_count ? '<span class="tag bad">' + esc(item.error_count) + "</span>"
                                                   : esc(item.error_count)) + "</td>" +
            "</tr>";
        }).join("") + "</tbody></table>");
    } catch (err) {
      message(node, "加载失败：" + err.message, true);
    }
  }

  // ---------------------------------------------------------------- 启动

  async function loadMode() {
    var badge = $("#mode-badge");
    try {
      var health = await loadHealth();
      badge.textContent = health.llm_mode === "live" ? "live（已配 Key）" : "mock（没配 Key，模板作答）";
      badge.className = "badge badge-" + (health.llm_mode === "live" ? "live" : "mock");
      badge.title = "知识库 " + health.kb_docs + " 篇 / " + health.kb_chunks +
        " 个片段 · 系统今天 " + health.today;
    } catch (err) {
      badge.textContent = "连不上服务";
      badge.className = "badge badge-unknown";
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    $("#chat-form").addEventListener("submit", function (event) {
      event.preventDefault();
      var input = $("#chat-input");
      var question = input.value.trim();
      if (!question) return;
      input.value = "";
      sendQuestion(question);
    });

    $("#chat-reset").addEventListener("click", function () {
      sessionId = newSessionId();
      $("#chat-log").innerHTML = "";
      message($("#chat-status"), "已开一段新对话，session：" + sessionId);
    });

    $("#dash-reload").addEventListener("click", loadDashboard);
    $("#dash-store").addEventListener("change", loadDashboard);
    $("#dash-start").addEventListener("change", loadDashboard);
    $("#dash-end").addEventListener("change", loadDashboard);

    $("#dash-quality").addEventListener("click", function () {});

    $("#debug-load").addEventListener("click", function () {
      loadTrace($("#debug-id").value.trim());
    });
    $("#debug-id").addEventListener("keydown", function (event) {
      if (event.key === "Enter") loadTrace($("#debug-id").value.trim());
    });
    $("#debug-recent").addEventListener("click", loadRecentTraces);

    // 问答与调试之间的跳转：不必让用户去地址栏里改 hash。
    document.addEventListener("click", function (event) {
      var target = event.target.closest("[data-trace]");
      if (!target) return;
      location.hash = "#/debug/" + encodeURIComponent(target.getAttribute("data-trace"));
    });

    window.addEventListener("hashchange", route);
    loadMode();
    route();
  });
})();
