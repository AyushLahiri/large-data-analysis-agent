// Saved chart plans bind to one source table. All chart interactions stay in the browser.
const chatCharts = (() => {
  const palettes = {
    Ocean: ['#17648a', '#b55516', '#26836b', '#8356a3', '#976e06', '#3679ad'],
    Woodland: ['#276345', '#a65326', '#456bb0', '#97536d', '#657020', '#735694'],
    Violet: ['#70489c', '#197b80', '#ad582a', '#3669a1', '#956917', '#526844'],
    Slate: ['#374e64', '#a24d45', '#538264', '#80669f', '#a57419', '#167c92'],
  };

  const widgets = new Map();
  let chat = '', library;

  /**
   * Load the bundled ECharts library on the first chart request.
   *
   * Returns: A shared promise that resolves when the script is ready.
   * Details: Concurrent cards share the load. A failed load clears the promise
   *     so a later request can retry.
   */
  function loadCharts() {
    if (!library) library = new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = '/static/vendor/echarts-6.0.0.min.js';
      script.onload = resolve;
      script.onerror = () => { library = null; script.remove(); reject(new Error('The chart library could not load.')); };
      document.head.append(script);
    });
    return library;
  }

  /**
   * Release the previous chat's chart cards when the selection changes.
   *
   * Inputs: id identifies the project and chat, or is empty on reset.
   * Returns: Nothing. Disposes mounted charts and clears the widget cache.
   */
  function setChat(id) {
    if (id === chat) return;
    for (const widget of widgets.values()) widget.dispose();
    widgets.clear();
    chat = id;
  }

  /**
   * Add a labeled group of view or palette buttons to the card.
   *
   * Inputs: parent, group label, button names, selected index and change callback.
   * Returns: Nothing. A click updates aria-pressed and passes the index to change.
   */
  function controls(parent, label, names, selected, change) {
    const row = el('div', undefined, 'viz-controls');
    row.setAttribute('role', 'group');
    row.setAttribute('aria-label', label);
    for (const [index, name] of names.entries()) {
      const button = el('button', name, 'viz-button');
      button.type = 'button';
      button.setAttribute('aria-pressed', String(index === selected));
      button.onclick = () => {
        for (const choice of row.children) choice.setAttribute('aria-pressed', String(choice === button));
        change(index);
      };
      row.append(button);
    }
    parent.append(row);
  }

  const array = value => Array.isArray(value) ? value : [value];
  const gradients = [
    ['#eff7fb','#17648a'], ['#eff7ee','#276345'], ['#f6f1fb','#70489c'], ['#f0f3f5','#374e64'],
  ];

  const defaultGrid = {top: 52, bottom: 60, left: 64, right: 30};
  const ruler = document.createElement('canvas').getContext('2d');
  /**
   * Fit centered horizontal axis names to the available plot width.
   *
   * Inputs: option is the chart design and width is the plot container's CSS width.
   * Returns: ECharts name updates in x-axis order.
   * Details: Measures words with the axis font and accounts for grid margins.
   *     On resize the caller uses the original names so wrapped lines can rejoin.
   */
  function xAxisNames(option, width) {
    const grids = array(option.grid || defaultGrid);
    const px = value => typeof value === 'number' ? value : width * parseFloat(value) / 100;
    return array(option.xAxis || []).map(axis => {
      let name = axis.name || '';
      if ((axis.nameLocation || 'middle') !== 'middle' || axis.nameRotate || !name) return {name};
      const grid = grids[axis.gridIndex || 0] || {};
      const left = px(grid.left ?? '10%'), right = px(grid.right ?? '10%');
      const span = grid.width == null ? width - left - right : px(grid.width);
      const available = span + (grids.length > 1 && grid.width != null ? 0 : 2 * Math.min(left, right)) - 12;
      if (!(available > 32)) return {name};
      const style = axis.nameTextStyle || {};
      ruler.font = `${style.fontWeight || 'normal'} ${style.fontSize || 12}px ${style.fontFamily || 'sans-serif'}`;
      name = name.split('\n').map(line => {
        if (ruler.measureText(line).width <= available) return line;
        const lines = [''];
        for (const word of line.split(/\s+/)) {
          const i = lines.length - 1, joined = lines[i] ? lines[i] + ' ' + word : word;
          if (lines[i] && ruler.measureText(joined).width > available) lines.push(word);
          else lines[i] = joined;
        }
        return lines.join('\n');
      }).join('\n');
      return {name};
    });
  }

  /**
   * Draw one saved view with the active palette and common chat styling.
   *
   * Inputs: node is the plot container, view holds a validated option and
   *     optional height, and paletteIndex selects the colors.
   * Returns: The ECharts instance that the card owns and disposes.
   * Flow: Copy the option, resolve palette colors, fit axis names and supply
   *     legend and tooltip defaults before rendering the SVG.
   */
  function draw(node, view, paletteIndex) {
    const colors = Object.values(palettes)[paletteIndex];
    const option = JSON.parse(JSON.stringify(view.option), (key, value) => {
      if (!/color$|^(fill|stroke)$/i.test(key)) return value;
      const resolve = color => typeof color === 'string' && /^palette\[[0-5]\]$/.test(color)
        ? colors[Number(color[8])] : color;
      return Array.isArray(value) ? value.map(resolve) : resolve(value);
    });
    option.color = colors;
    option.backgroundColor = '#fff';
    option.animation = false;
    option.useUTC = true;
    option.aria = {...option.aria, enabled: true};
    if (option.xAxis || option.yAxis) option.grid ??= defaultGrid;
    const names = xAxisNames(option, node.clientWidth);
    array(option.xAxis || []).forEach((axis, i) => { axis.name = names[i].name; });
    for (const key of ['xAxis', 'yAxis']) {
      for (const axis of array(option[key] || [])) {
        axis.nameLocation ??= key === 'xAxis' ? 'middle' : 'end';
        axis.nameGap ??= key === 'xAxis' ? 32 : 14;
        if (key === 'yAxis') axis.nameTextStyle = {align: 'left', ...axis.nameTextStyle};
        if (key === 'xAxis') axis.splitLine = {show: true, interval: 0,
          lineStyle: {color: 'rgba(87,102,117,.27)', type: 'dotted'}};
      }
    }
    const groups = new Map();
    for (const s of option.series) {
      const axis = array(option.xAxis || [])[s.xAxisIndex || 0];
      const panel = axis?.gridIndex || 0;
      if (!groups.has(panel)) groups.set(panel, new Set());
      if (s.name) groups.get(panel).add(s.name);
    }
    if (!option.legend && [...groups.values()].some(group => group.size > 1)) {
      option.legend = {type: 'scroll', top: 0};
    }
    for (const scale of array(option.visualMap || [])) {
      const sequential = gradients[paletteIndex];
      const diverging = scale.min < 0 && scale.max > 0 && scale.min === -scale.max;
      scale.inRange = {...scale.inRange, color: diverging ? [colors[1], '#f6f6f3', colors[0]] : sequential};
    }
    option.tooltip = {...option.tooltip, show: true, renderMode: 'richText', confine: true};
    if (!option.tooltip.formatter) option.tooltip.formatter = (...args) => {
      const items = array(args[0]);
      return items.map(p => {
        const d = p.data || {};
        const raw = p.value;
        let value = Array.isArray(raw) ? raw.join(' · ') : String(raw ?? '—');
        if (Array.isArray(raw) && p.encode?.tooltip?.length) {
          value = p.encode.tooltip.map(i => (p.dimensionNames?.[i] ? p.dimensionNames[i] + ': ' : '') + (raw[i] ?? '—')).join('\n');
        }
        const name = p.seriesName && !p.seriesName.startsWith('series\u0000') ? p.seriesName : '';
        const body = d.hover || [p.name, [name, value].filter(Boolean).join(': ')].filter(Boolean).join('\n');
        return [body, d.annotation].filter(Boolean).join('\n');
      }).join('\n');
    };
    const rows = new Set(array(option.grid || []).map(grid => grid.top ?? 0)).size;
    node.style.height = (view.height || (rows > 1 ? 210 * rows : 320)) + 'px';
    const chart = echarts.init(node, null, {renderer: 'svg'});
    try { chart.setOption(option); } catch (error) { chart.dispose(); throw error; }
    return chart;
  }
  /**
   * Mount the interactive card used in chat and in the worker's draft preview.
   *
   * Inputs: card is the container, views are the alternatives, and renderTable
   *     optionally builds the source table when its disclosure first opens.
   * Returns: A cleanup function for the resize observer and current chart.
   * Details: One palette selection applies across the views. A redraw replaces
   *     the active plot and a resize refits its labels.
   */
  function mount(card, views, renderTable) {
    const title = el('h3'), toolbar = el('div', undefined, 'viz-toolbar');
    const canvas = el('div', undefined, 'viz-plot'), note = el('p', undefined, 'viz-note');
    const source = el('details', undefined, 'viz-source');
    source.append(el('summary', 'View associated table'));
    source.ontoggle = () => {
      if (source.open && source.childElementCount === 1 && renderTable) source.append(renderTable());
    };
    card.replaceChildren(title, toolbar, canvas, note, source);
    let active = 0, palette = 0, chart;
    function drawActive() {
      chart?.dispose();
      const view = views[active];
      title.textContent = view.title;
      note.textContent = view.note;
      note.hidden = !view.note;
      chart = draw(canvas, view, palette);
    }
    if (views.length > 1) {
      const labels = views.map((view, i) => {
        const sameKind = v => v.kind === view.kind;
        return view.kind + (views.filter(sameKind).length > 1 ? ' ' + views.slice(0, i + 1).filter(sameKind).length : '');
      });
      controls(toolbar, 'Chart views', labels, active, index => { active = index; drawActive(); });
    }
    const paletteRow = el('div', undefined, 'viz-palettes');
    paletteRow.append(el('span', 'Palette'));
    toolbar.append(paletteRow);
    controls(paletteRow, 'Color palette', Object.keys(palettes), palette, index => { palette = index; drawActive(); });
    drawActive();
    const observer = new ResizeObserver(() => {
      const width = canvas.clientWidth;
      if (!card.isConnected || !width || width === chart.getWidth()) return;
      chart.resize();
      const axes = xAxisNames(views[active].option, width);
      if (axes.length) chart.setOption({xAxis: axes});
    });
    observer.observe(canvas);
    return () => { observer.disconnect(); chart.dispose(); };
  }

  /**
   * Get a chart card for a published visualization in the selected chat.
   *
   * Inputs: url addresses the publication turn, tableId identifies its source
   *     and renderTable displays the returned table.
   * Returns: The card element, initially showing loading status on a cache miss.
   * Flow: Fetch the saved views and table, verify the ID and mount the card.
   *     Polling reuses the cached element and its selected view and palette.
   */
  function panel(url, tableId, renderTable) {
    if (widgets.has(url)) return widgets.get(url).card;
    const card = el('section', undefined, 'viz-card');
    card.dataset.tableId = tableId;
    card.append(el('p', 'Loading visualization…'));
    let disposed = false, unmount;
    widgets.set(url, {card, dispose() { disposed = true; unmount?.(); }});
    (async () => {
      const {table, views} = await api(url);
      if (table.table_id !== tableId) throw new Error('The chart source could not be matched.');
      await loadCharts();
      if (!disposed) unmount = mount(card, views, () => renderTable(table));
    })().catch(error => { if (!disposed) card.replaceChildren(el('p', error.message)); });
    return card;
  }
  return {panel, setChat, mount};
})();
