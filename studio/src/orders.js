// OpenStall seller orders, on AG Grid (with the AG Grid AI Toolkit driven by Claude).
// Build: npm run build (outputs ../app/static/studio/orders.js).
import { createGrid, ModuleRegistry, AllCommunityModule, themeQuartz } from 'ag-grid-community';
import {
  LicenseManager,
  RowGroupingModule,
  SetFilterModule,
  SideBarModule,
  ColumnsToolPanelModule,
  FiltersToolPanelModule,
  ColumnMenuModule,
  ContextMenuModule,
  AiToolkitModule,
  RowGroupingPanelModule,
} from 'ag-grid-enterprise';

const cfgEl = document.getElementById('orders-config');
if (cfgEl) boot(JSON.parse(cfgEl.textContent));

function boot(cfg) {
  // AG Grid Enterprise needs an AG Grid key: an AG Studio key does not cover a standalone grid and shows
  // "Invalid License". With no key AG Grid runs in its free evaluation mode ("For Trial Use Only").
  // Only grid modules are registered (no integrated charts), so a Grid-only key is enough.
  if (cfg.licenseKey) LicenseManager.setLicenseKey(cfg.licenseKey);
  ModuleRegistry.registerModules([AllCommunityModule, RowGroupingModule, SetFilterModule, SideBarModule,
    ColumnsToolPanelModule, FiltersToolPanelModule, ColumnMenuModule, ContextMenuModule, AiToolkitModule, RowGroupingPanelModule]);
  const ent = true;
  const setF = 'agSetColumnFilter';
  const grp = { enableRowGroup: true };
  const agg = (fn) => ({ aggFunc: fn, enableValue: true });
  const host = document.getElementById('orders-grid');
  const fallback = document.getElementById('orders-fallback');
  const enc = encodeURIComponent;
  const isMarket = !!cfg.marketId;  // organizer view: every stall's orders, read-only
  const base = isMarket ? `/api/markets/${enc(cfg.marketId)}` : `/api/stalls/${enc(cfg.stallId)}`;
  const scope = isMarket ? `market=${enc(cfg.marketId)}` : `stall=${enc(cfg.stallId)}`;
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  const STATUS_CLASS = { paid: 'ok', ready: 'ok', picked_up: 'done', shipped: 'done', refunded: 'muted', pending_payment: 'wait',
    capturing: 'wait', payment_review: 'warn', needs_attention: 'bad', payment_failed: 'bad', sold_out: 'bad', cancelled: 'muted', expired: 'muted' };

  const toRow = (o) => ({
    stall: o.stall_name,
    order_id: o.order_id,
    pickup_date: o.pickup_date,
    pickup_window: o.pickup_window,
    items: (o.lines || []).map((l) => `${l.quantity} × ${l.name}`).join(', '),
    units: (o.lines || []).reduce((n, l) => n + (l.quantity || 0), 0),
    total: (o.total_cents || 0) / 100,
    channel: o.channel === 'agent' ? 'AI agent' : 'Web',
    market_run: o.market_order_id ? 'Market run' : 'Single stall',
    status: o.status,
    status_label: o.status_label,
    paid_to: o.paid_to || '',
  });

  class ActionsRenderer {
    init(p) {
      this.el = document.createElement('div');
      this.el.className = 'grid-actions';
      const d = p.data;
      if (!d) return;
      const btn = (action, label, danger) => `<button class="btn small${danger ? ' danger' : ''}" data-action="${action}" data-order="${esc(d.order_id)}">${label}</button>`;
      let html = '';
      if (d.status === 'needs_attention') html += btn('refund', 'Retry refund', true);
      if (d.status === 'paid') html += btn('ready', 'Mark ready');
      if (d.status === 'paid' || d.status === 'ready') {
        html += btn('picked_up', 'Picked up');
        if (cfg.ships) html += btn('ship', 'Tracking');
        html += btn('refund', 'Refund', true);
      }
      this.el.innerHTML = html;
    }
    getGui() { return this.el; }
    refresh() { return false; }
  }

  const pill = (cls) => (p) => (p.value ? `<span class="gpill ${cls(p)}">${esc(p.value)}</span>` : '');

  const columnDefs = [
    ...(isMarket ? [{ field: 'stall', headerName: 'Stall', width: 210, filter: setF, ...grp, pinned: 'left' }] : []),
    { field: 'pickup_date', headerName: 'Pickup', filter: 'agDateColumnFilter', width: 150, ...grp, sort: 'asc',
      filterParams: { comparator: (d, v) => (v < d.toISOString().slice(0, 10) ? -1 : v > d.toISOString().slice(0, 10) ? 1 : 0) } },
    { field: 'pickup_window', headerName: 'Window', width: 125, ...grp },
    { field: 'order_id', headerName: 'Order', width: 150, cellRenderer: (p) => (p.value ? `<a href="/order/${esc(p.value)}" target="_blank" rel="noopener">${esc(p.value)}</a>` : '') },
    { field: 'items', headerName: 'Items', flex: 1, minWidth: 220, wrapText: true, autoHeight: true },
    { field: 'units', headerName: 'Units', width: 110, type: 'numericColumn', ...agg('sum') },
    { field: 'total', headerName: 'Total', width: 110, type: 'numericColumn', ...agg('sum'),
      valueFormatter: (p) => (p.value == null ? '' : `$${Number(p.value).toFixed(2)}`) },
    { field: 'channel', headerName: 'Via', width: 120, filter: setF, ...grp,
      cellRenderer: pill((p) => (p.value === 'AI agent' ? 'agent' : 'web')) },
    { field: 'market_run', headerName: 'Basket', width: 140, filter: setF, ...grp,
      cellRenderer: pill((p) => (p.value === 'Market run' ? 'market' : 'muted')) },
    { field: 'status_label', headerName: 'Status', width: 170, filter: setF, ...grp,
      cellRenderer: (p) => (p.value ? `<span class="gpill ${STATUS_CLASS[p.data?.status] || ''}">${esc(p.value)}</span>` : '') },
    { field: 'paid_to', headerName: 'Paid to (PayPal)', width: 250 },
    ...(isMarket ? [] : [{ colId: 'actions', headerName: 'Actions', width: 300, sortable: false, filter: false, cellRenderer: ActionsRenderer, suppressHeaderMenuButton: true, pinned: 'right' }]),
  ];

  const theme = themeQuartz.withParams({
    accentColor: '#c2410c', fontFamily: 'Inter, system-ui, sans-serif', headerFontWeight: 600,
    backgroundColor: '#ffffff', headerBackgroundColor: '#fbf7f0', borderColor: '#e8dfd2', rowHoverColor: '#fdf6ee', fontSize: 14,
  });

  const api = createGrid(host, {
    theme,
    columnDefs,
    rowData: [],
    getRowId: (p) => p.data.order_id,
    defaultColDef: { sortable: true, filter: true, resizable: true, floatingFilter: false, enableCellChangeFlash: true },
    ...(ent ? {
      rowGroupPanelShow: 'onlyWhenGrouping',
      groupDefaultExpanded: 1,
      grandTotalRow: 'bottom',
      autoGroupColumnDef: { headerName: 'Group', minWidth: 220 },
      sideBar: { toolPanels: ['columns', 'filters'], hiddenByDefault: false, defaultToolPanel: '' },
    } : {}),
    suppressColumnVirtualisation: true,
    domLayout: 'autoHeight',
    pagination: false,
    animateRows: true,
    overlayNoRowsTemplate: '<span class="muted">No orders yet. Share your stall or ask an AI agent to find you.</span>',
  });
  if (fallback) fallback.hidden = true;
  host.hidden = false;

  // ---------------------------------------------------------- live updates
  let known = new Map();
  async function refresh() {
    const res = await fetch(`${base}/orders?key=${enc(cfg.key)}`);
    if (!res.ok) return;
    if (fallback) fallback.hidden = true;
    const rows = (await res.json()).orders.map(toRow);
    const add = [], update = [];
    for (const r of rows) {
      const prev = known.get(r.order_id);
      if (!prev) add.push(r);
      else if (JSON.stringify(prev) !== JSON.stringify(r)) update.push(r);
    }
    const ids = new Set(rows.map((r) => r.order_id));
    const remove = [...known.values()].filter((r) => !ids.has(r.order_id));
    known = new Map(rows.map((r) => [r.order_id, r]));
    if (add.length || update.length || remove.length) {
      const tx = api.applyTransaction({ add, update, remove });
      const flash = [...(tx?.add || []), ...(tx?.update || [])];
      if (flash.length && known.size !== add.length) api.flashCells({ rowNodes: flash });
    }
    document.getElementById('orders-count').textContent = `${rows.length} order${rows.length === 1 ? '' : 's'}`;
  }
  refresh();
  setInterval(refresh, 5000);

  // ------------------------------------------------------ inline actions
  host.addEventListener('click', async (e) => {
    const b = e.target.closest('button[data-action]');
    if (!b) return;
    const action = b.dataset.action;
    const body = { key: cfg.key, action };
    if (action === 'refund' && !window.confirm('Refund this order through PayPal? This cannot be undone.')) return;
    if (action === 'ship') {
      body.tracking_number = window.prompt('Tracking number?') || '';
      if (!body.tracking_number) return;
      body.carrier = window.prompt('Carrier (USPS, UPS, FEDEX)?', 'USPS') || 'USPS';
    }
    b.disabled = true;
    const res = await fetch(`${base}/orders/${enc(b.dataset.order)}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    if (!res.ok) { const d = await res.json().catch(() => ({})); window.alert(d.message || 'Action failed'); b.disabled = false; }
    refresh();
  });

  // ------------------------------------------------------ toolbar
  document.getElementById('orders-quick').addEventListener('input', (e) => api.setGridOption('quickFilterText', e.target.value));
  document.getElementById('orders-csv').addEventListener('click', () => api.exportDataAsCsv({
    fileName: `${cfg.marketId || cfg.stallId}-orders.csv`, columnKeys: columnDefs.filter((c) => c.field).map((c) => c.field),
  }));
  document.getElementById('orders-reset').addEventListener('click', () => {
    api.setState({}, []); api.setGridOption('quickFilterText', ''); document.getElementById('orders-quick').value = '';
    api.applyColumnState({ state: [{ colId: 'pickup_date', sort: 'asc' }], defaultState: { sort: null, rowGroup: false, hide: false } });
    say('');
  });

  // ------------------------------------------- AI Toolkit: ask the grid in plain words (Claude)
  const form = document.getElementById('orders-ask');
  const out = document.getElementById('orders-ask-out');
  const say = (t, cls = '') => { out.textContent = t; out.className = `ask-out ${cls}`; };
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const input = form.querySelector('input');
    const request = input.value.trim();
    if (!request) return;
    const btn = form.querySelector('button');
    btn.disabled = true; say('Thinking...');
    try {
      const schema = api.getStructuredSchema({
        exclude: ['pivot', 'columnSizing'],
        columns: {
          channel: { description: 'Who placed the order: "AI agent" or "Web"', includeSetValues: ent },
          ...(isMarket ? { stall: { description: 'Which stall (seller) the order belongs to', includeSetValues: ent } } : {}),
          market_run: { description: '"Market run" when bought with other stalls in one PayPal payment', includeSetValues: ent },
          status_label: { description: 'Order status shown to the seller', includeSetValues: ent },
          pickup_date: { description: 'Pickup date, YYYY-MM-DD' },
          total: { description: 'Order total in USD' },
          paid_to: { description: 'PayPal account that received the payment' },
        },
      });
      const res = await fetch(`/api/grid/command?${scope}&key=${enc(cfg.key)}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ request, state: api.getState(), schema, today: new Date().toISOString().slice(0, 10) }),
      });
      const data = await res.json();
      if (!res.ok || data.error) throw new Error(data.message || data.error || 'The assistant could not change the grid.');
      api.setState(data.gridState || {}, data.propertiesToIgnore || []);
      say(data.explanation || 'Done.', 'ok');
    } catch (err) {
      say(String(err.message || err), 'bad');
    } finally {
      btn.disabled = false;
    }
  });
  document.querySelectorAll('[data-ask]').forEach((a) => a.addEventListener('click', () => {
    form.querySelector('input').value = a.dataset.ask; form.requestSubmit();
  }));

  window.openstallOrdersGrid = api;
}
