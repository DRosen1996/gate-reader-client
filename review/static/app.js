'use strict';

// All source and user text goes through textContent, including JSON details.
let data;
const $ = id => document.getElementById(id);

function el(tag, text) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  return element;
}

function date(timestamp) {
  return timestamp === null || timestamp === undefined
    ? '未知'
    : new Date(Number(timestamp) * 1000).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai'}) + ' 北京时间';
}

function source(parent, title, value) {
  const detail = el('details');
  detail.append(el('summary', title), el('pre', JSON.stringify(value, null, 2)));
  parent.append(detail);
}

function list() {
  const root = $('list');
  root.replaceChildren();
  const filter = $('filter').value.toUpperCase();
  const rows = data.cycles.filter(cycle => cycle.contract.includes(filter));
  if (!rows.length) root.append(el('p', '暂无交易，或筛选无结果。历史计划：未知。'));
  for (const cycle of rows) {
    const article = el('article');
    article.append(
      el('h2', cycle.contract),
      el('p', `${date(cycle.opened)} → ${date(cycle.closed)} · ${cycle.closed === null ? '持仓中' : '已闭仓（已读范围推导）'}`),
      el('p', `已归属交易净现金流 ${cycle.trading_cash_net ?? '未知'} · 暂计`),
    );
    const button = el('button', '展开单笔回放');
    button.onclick = () => { location.hash = encodeURIComponent(cycle.id); };
    article.append(button);
    root.append(article);
  }
}

function render() {
  // A manually edited malformed URI fragment must not break refresh/back.
  let selected;
  try { selected = decodeURIComponent(location.hash.slice(1)); }
  catch { selected = ''; }
  const cycle = data.cycles.find(row => row.id === selected);
  $('list').hidden = !!cycle;
  $('detail').hidden = !cycle;
  $('filter').parentElement.hidden = !!cycle;
  if (!cycle) { list(); return; }

  const root = $('detail');
  root.replaceChildren();
  const back = el('button', '返回交易列表');
  back.onclick = () => { location.hash = ''; };
  root.append(
    back,
    el('h2', cycle.contract + ' · 单笔回放'),
    el('p', '已读取区间内持仓推导 · 暂计'),
    el('p', cycle.plan),
    el('p', '无独立行情源；K 线、MAE / MFE：未知'),
    el('h3', '计划、决策与复盘版本'),
  );
  for (const note of cycle.notes) {
    root.append(el('p', `${note.kind} · ${date(note.recorded)} · ${note.retrospective ? '事后补记' : '记录时计划'}：${note.body}`));
  }
  const kind = el('select');
  kind.setAttribute('aria-label', '记录类型');
  for (const [value, title] of [
    ['plan', '计划修订（历史交易为事后补记）'],
    ['decision', '决策记录'],
    ['review', '事后复盘'],
  ]) {
    const option = el('option', title);
    option.value = value;
    kind.append(option);
  }
  const text = el('textarea');
  text.setAttribute('aria-label', '修订内容');
  text.maxLength = 10000;
  const save = el('button', '保存新版本');
  const message = el('p');
  message.setAttribute('role', 'status');
  save.onclick = async () => {
    save.disabled = true;
    try {
      const response = await fetch('/api/notes', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({cycle: cycle.id, kind: kind.value, body: text.value}),
      });
      if (!response.ok) throw Error('Save failed');
      await load();
    } catch {
      message.textContent = '保存失败，请检查输入后重试';
      save.disabled = false;
    }
  };
  root.append(kind, text, save, message, el('h3', '订单、成交、费用和退出时间线'));
  source(root, '订单及当前观察状态', {latest: cycle.orders, versions: cycle.order_versions});
  for (const fill of cycle.fills) {
    const article = el('article');
    article.append(el('p', `${date(fill.source.create_time)} · 分配成交数量 ${fill.allocated_size} · 价格 ${fill.source.price}`));
    source(article, '展开成交来源', fill.source);
    root.append(article);
  }
  source(root, '资金归属与已实现分项（暂计）', {flows: cycle.flows, components: cycle.realised_components});
  source(root, '费用来源分配（仅作证据，不重复扣减）', cycle.fees);
  root.append(el('p', `退出时间：${date(cycle.closed)} · 已实现成交盈亏（pnl）${cycle.realised_pnl ?? '未知'} · 已归属交易净现金流 ${cycle.trading_cash_net ?? '未知'} · 暂计`));
}

async function load() {
  try {
    const response = await fetch('/api/view');
    if (!response.ok) throw Error('Read failed');
    data = await response.json();
    $('status').replaceChildren(
      el('p', data.synthetic ? '合成演示数据 · 无真实账户' : '私有本地账本'),
      el('p', '连续历史数据截至：' + date(data.as_of)),
      el('p', data.completeness),
      el('p', `已读流水小计（${data.currency}）：已实现成交盈亏 ${data.realised_pnl}；费用 ${data.cash_categories.fee}；资金费 ${data.cash_categories.fund}；交易净现金流 ${data.period_trading_cash_net} · 暂计`),
      el('p', `入金 ${data.deposits}；出金 ${data.withdrawals}（来源符号）· 与交易盈亏分开`),
    );
    source($('status'), '余额桥接（零残差不证明完整）', data.balance_bridge);
    source($('status'), '同步范围与缺口', {coverage: data.coverage, ranges: data.ranges, gaps: data.gaps});
    source($('status'), '未分配成交、费用与流水', data.unallocated);
    source($('status'), '未找到可确认周期的历史笔记', data.orphan_notes);
    source($('status'), '账户与仓位来源快照（未实现盈亏独立展示）', {account: data.account, positions: data.positions});
    render();
  } catch {
    $('status').textContent = '读取失败。请刷新重试；未加载数据不表示没有交易。';
  }
}

$('filter').oninput = list;
window.onhashchange = () => { if (data) render(); };
load();
