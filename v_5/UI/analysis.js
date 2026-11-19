(() => {
  let folder = '', snapshot = null, treeKey = '', chatKey = '';
  let checking = null, submittingQuestion = false, greetingRequested = false;
  let stopRequest = '';
  let chats = null, chatId = '', chatListKey = '', deletingChat = null;
  let feedbackTurn = '';
  let requestedTree = '', treeDetails = null;
  let turnsKey = '', activeTurn = '', latestMessage = '';
  const folderOpen = new Map();
  const smallSampleLimit = 60;

  /**
   * Identify the publication currently displayed by analysis.
   *
   * Inputs: Reads projectId and Atlas release/table records.
   * Returns: JSON signature string.
   * Details: Used by loadTree/renderScope to invalidate asset details when publication
   *     changes.
   */
  function publicationKey() {
    const atlas = state?.atlas;
    return JSON.stringify([projectId, atlas?.release, atlas?.data || []]);
  }

  /**
   * Fetch richer asset details for the current publication.
   *
   * Inputs: id: project ID; reads publicationKey.
   * Returns: Promise with no value; updates treeDetails and rerenders scope if still
   *     current.
   * Details: Fetches once per publication signature. On failure, the publication receipt
   *     still supplies folder names and row counts.
   */
  async function loadTree(id) {
    const key = publicationKey();
    if (key === requestedTree) return;
    requestedTree = key;
    treeDetails = null;
    try {
      const details = await api(`/api/projects/${id}/analysis/tree`);
      if (key === publicationKey()) {
        treeDetails = details;
        renderScope();
      }
    } catch (_) {
      // The publication receipt still provides the folder names and row counts.
    }
  }

  /**
   * Render the selectable project/folder analysis scope.
   *
   * Inputs: Reads publication tables, optional treeDetails and folder labels.
   * Returns: Nothing; updates radio choices, asset counts and summary link.
   * Details: Groups physical table parts by folder, preserves expansion state and clears a
   *     selection that no longer exists.
   */
  function renderScope() {
    const atlas = state?.atlas;
    const grouping = new Map();
    for (const table of atlas?.data || []) {
      if (!grouping.has(table.folder)) grouping.set(table.folder, []);
      grouping.get(table.folder).push(table);
    }
    if (folder && !grouping.has(folder)) folder = '';
    const details = requestedTree === publicationKey() ? treeDetails : null;
    const key = JSON.stringify([atlas?.data, details, state?.final_summary_path, state?.folders]);
    if (key === treeKey) return;
    treeKey = key;
    const tree = $('analysis-tree');
    tree.replaceChildren();
    /**
     * Create a radio choice for an analysis scope.
     *
     * Inputs: name: folder key or blank for project; label: display text.
     * Returns: Unattached labeled radio control.
     * Details: Nested in renderScope; changing it updates folder for the next question.
     */
    function option(name, label) {
      const choice = el('label', undefined, 'scope-choice');
      const toggle = el('input');
      toggle.type = 'radio';
      toggle.name = 'analysis-scope';
      toggle.value = name;
      toggle.checked = folder === name;
      toggle.onchange = () => { folder = name; };
      choice.append(toggle, el('span', label));
      return choice;
    }
    tree.append(option('', 'Entire project'));
    const branches = el('div', undefined, 'scope-branches');
    for (const [index, [name, parts]] of [...grouping.entries()].entries()) {
      const label = state.folders[name]?.display_name || parts[0].dataset_name || name;
      const branch = el('div', undefined, 'scope-folder');
      const head = el('div', undefined, 'scope-folder-head');
      const expander = el('button', '›', 'scope-expander');
      expander.type = 'button';
      const children = el('ul', undefined, 'scope-artifacts');
      children.id = `scope-folder-${index}`;
      expander.setAttribute('aria-controls', children.id);
      /**
       * Update one folder branch's visible and accessible expansion state.
       *
       * Inputs: open: boolean; captures branch children, expander and label.
       * Returns: Nothing; changes visibility and accessibility attributes.
       * Details: Nested in renderScope; the click handler stores the same state in folderOpen.
       */
      function setOpen(open) {
        children.hidden = !open;
        expander.setAttribute('aria-expanded', String(open));
        expander.setAttribute('aria-label', `${open ? 'Collapse' : 'Expand'} ${label}`);
      }
      setOpen(folderOpen.get(name) ?? true);
      expander.onclick = () => {
        folderOpen.set(name, children.hidden);
        setOpen(children.hidden);
      };
      const info = details?.[name];
      const rows = (info?.row_count ?? parts[0].row_count).toLocaleString();
      const shape = Number.isInteger(info?.column_count) ? `${rows} × ${info.column_count.toLocaleString()}` : `${rows} rows`;
      const raw = el('li', undefined, 'scope-artifact');
      raw.append(el('code', 'raw_data'), el('span', `(${shape})`, 'muted'));
      children.append(raw);
      for (const [field, caption] of [['codebook', 'Codebook'], ['unmatched', 'Unmatched codebook'], ['soft_matched', 'Soft-matched codebook'],
                                      ['information', 'General information'], ['guide', 'Folder notes']]) {
        if (info?.[field]) children.append(el('li', caption, 'scope-artifact'));
      }
      head.append(expander, option(name, label));
      branch.append(head, children);
      branches.append(branch);
    }
    if (state.final_summary_path) {
      const link = el('a', 'Project data summary', 'scope-summary');
      link.href = endpoint(`download/${state.final_summary_path}`);
      branches.append(link);
    }
    tree.append(branches);
  }

  /**
   * Recognize a header describing an unweighted sample count.
   *
   * Inputs: header: column caption.
   * Returns: Boolean match.
   * Details: Fallback for markSmallSamples when explicit unweighted_sample_columns are
   *     absent; excludes weighted/effective measures.
   */
  function unweightedSampleHeader(header) {
    const label = header.toLowerCase().replace(/[_-]/g, ' ');
    return !/\b(?:weighted|effective)\b/.test(label) && (
      /\bsample size\b/.test(label) ||
      (/\bunweighted\b/.test(label) && /\b(?:n|counts?|samples?|base|denominator|respondents|observations|records)\b/.test(label))
    );
  }

  /**
   * Mark low unweighted counts in a rendered result table.
   *
   * Inputs: table: DOM table; result: optional columns/rows and unweighted_sample_columns
   *     payload.
   * Returns: Nothing; wraps qualifying cells and adds an explanatory note.
   * Details: Called for structured and Markdown tables. Only nonnegative integer counts
   *     below smallSampleLimit are marked.
   */
  function markSmallSamples(table, result) {
    const headers = Array.from(table.tHead?.rows[0]?.cells || [], cell => cell.textContent.trim());
    const sampleColumns = new Set(result?.unweighted_sample_columns?.map(header => header.trim()) ?? headers.filter(unweightedSampleHeader));
    if (!sampleColumns.size) return;

    let flagged = false;
    for (const [rowIndex, row] of Array.from(table.tBodies[0]?.rows || []).entries()) {
      for (const [columnIndex, cell] of Array.from(row.cells).entries()) {
        if (!sampleColumns.has(headers[columnIndex])) continue;
        const value = result ? result.rows[rowIndex][columnIndex] : cell.textContent.trim();
        const count = typeof value === 'string' && /^\d+(?:\.0+)?$/.test(value.trim()) ? Number(value) : value;
        if (!Number.isInteger(count) || count < 0 || count >= smallSampleLimit) continue;
        const mark = el('span', undefined, 'analysis-small-sample');
        mark.append(...cell.childNodes);
        cell.append(mark);
        flagged = true;
      }
    }
    if (flagged) {
      const note = el('div', undefined, 'analysis-small-sample-note');
      const key = el('span', undefined, 'analysis-small-sample-key');
      key.setAttribute('aria-hidden', 'true');
      note.append(key, el('span', `Sample size is less than ${smallSampleLimit}. Interpret results cautiously. Statistical interpretation may be unreliable.`));
      table.parentElement.after(note);
    }
  }

  /**
   * Build a formatted analysis result table.
   *
   * Inputs: result: title, columns, rows and optional sample-column metadata; decimalPlaces:
   *     precision or null.
   * Returns: Unattached result container.
   * Details: Formats numeric cells, avoids rounding tiny positive p-values to zero and calls
   *     markSmallSamples. Original result values stay unchanged.
   */
  function renderTable(result, decimalPlaces = 2) {
    const resultView = el('div', undefined, 'analysis-result');
    const wrap = el('div', undefined, 'analysis-table-wrap');
    const table = el('table', undefined, 'analysis-table');
    if (result.title) table.append(el('caption', result.title));
    const head = el('thead'), header = el('tr');
    for (const title of result.columns) {
      const cell = el('th', title); cell.scope = 'col'; header.append(cell);
    }
    head.append(header);
    const body = el('tbody');
    const format = decimalPlaces === null || decimalPlaces > 20 ? null : new Intl.NumberFormat(undefined, {
      maximumFractionDigits: decimalPlaces, useGrouping: false,
    });
    for (const row of result.rows) {
      const line = el('tr');
      for (const [index, value] of row.entries()) {
        let displayed = value === null ? '—' : String(value);
        if (typeof value === 'number' && format) {
          displayed = format.format(value);
          const pValue = /^(?:adjusted\s+)?p[- ]?value\b/i.test(result.columns[index]);
          if (pValue && value > 0 && value < 0.5 * 10 ** -decimalPlaces) {
            displayed = '< ' + format.format(10 ** -decimalPlaces);
          }
        }
        line.append(el('td', displayed));
      }
      body.append(line);
    }
    table.append(head, body); wrap.append(table); resultView.append(wrap);
    markSmallSamples(table, result);
    return resultView;
  }

  /**
   * Build the method and variables panel for a computed answer.
   *
   * Inputs: message: answer with details.method and analysis/table metadata.
   * Returns: Disclosure element, or null when no usable method is present.
   * Details: Handles structured and older text methods, limits displayed steps and uses
   *     formattedMessage for readable bullets.
   */
  function renderMethod(message) {
    const { method, ran_analysis, tables } = message.details || {};
    if (!method || !(ran_analysis ?? tables?.length)) return null;
    let steps = method.steps || [];
    if (typeof method === 'string') {
      steps = method.trim().split(/\n+/);
      if (steps.length === 1) steps = steps[0].split(/(?<=[.!?])\s+(?=[A-Z])/);
    }
    const bullets = steps.map(step => step.trim().replace(/^(?:[-*•]|\d+[.)]|\(\d+\))\s+/, '')).filter(Boolean).slice(0, 8);
    const variables = method.variables || [];
    if (!bullets.length && !variables.length) return null;

    const panel = disclosure(`analysis-method-${message.id}`, 'Methodology', 'analysis-method');
    if (bullets.length) {
      const list = el('ul', undefined, 'method-steps');
      for (const step of bullets) {
        const item = el('li');
        item.append(formattedMessage(step, 'method-step'));
        list.append(item);
      }
      panel.append(list);
    }
    if (variables.length) {
      const section = disclosure(`analysis-variables-${message.id}`, 'Variables used', 'method-variables');
      const definitions = el('dl');
      for (const variable of variables) {
        const term = el('dt');
        term.append(el('code', variable.name));
        const row = el('div');
        row.append(term, el('dd', variable.description));
        definitions.append(row);
      }
      section.append(definitions);
      panel.append(section);
    }
    return panel;
  }

  /**
   * Save a turn rating and refresh its displayed state.
   *
   * Inputs: turnId: answer key; rating: up/down; comment: optional reason.
   * Returns: Promise with no value; updates the matching snapshot turn.
   * Details: Posts the feedback endpoint and discards a late response after project/chat
   *     switching.
   */
  async function saveFeedback(turnId, rating, comment) {
    const id = projectId, chat = chatId;
    const feedback = await api(endpoint(`analysis/chats/${chat}/turns/${turnId}/feedback`), {rating, comment});
    if (projectId !== id || chatId !== chat) return;
    const turn = snapshot?.turns.find(item => item.id === turnId);
    if (turn) turn.feedback = feedback;
    renderChat();
  }

  /**
   * Build the answer-rating controls for one turn.
   *
   * Inputs: turn: current feedback and turn ID.
   * Returns: Unattached thumbs-up/down control group.
   * Details: Up submits through saveFeedback; down opens the comment dialog. Current rating
   *     is exposed through accessibility attributes.
   */
  function renderFeedback(turn) {
    const actions = el('div', undefined, 'analysis-feedback');
    actions.setAttribute('role', 'group');
    actions.setAttribute('aria-label', 'Rate this answer');
    const up = button('👍︎', async () => {
      up.disabled = true;
      try {
        await saveFeedback(turn.id, 'up');
      } catch (error) {
        $('analysis-error').textContent = error.message;
      } finally {
        up.disabled = false;
      }
    }, 'analysis-rating up');
    const down = button('👎︎', () => {
      feedbackTurn = turn.id;
      $('analysis-feedback-comment').value = turn.feedback?.comment || '';
      $('analysis-feedback-error').textContent = '';
      $('analysis-feedback-dialog').showModal();
      $('analysis-feedback-comment').focus();
    }, 'analysis-rating down');
    up.setAttribute('aria-label', 'Thumbs up');
    down.setAttribute('aria-label', 'Thumbs down');
    up.setAttribute('aria-pressed', String(turn.feedback?.rating === 'up'));
    down.setAttribute('aria-pressed', String(turn.feedback?.rating === 'down'));
    actions.append(up, down);
    return actions;
  }

  /**
   * Highlight a historical turn and its navigator marker.
   *
   * Inputs: id: selected turn ID, or blank to clear.
   * Returns: Nothing; updates activeTurn and selection attributes.
   * Details: Shared by scrolling, navigation and question submission so the chat and
   *     navigator remain in sync.
   */
  function selectTurn(id) {
    activeTurn = id;
    const anchor = $(`analysis-turn-${id}`);
    const parent = anchor?.closest('.analysis-turn-group');
    $('analysis-chat').querySelectorAll('.analysis-turn-group').forEach(group => {
      group.classList.toggle('is-selected', group === parent);
    });
    $('analysis-turn-list').querySelectorAll('button').forEach(marker => {
      marker.setAttribute('aria-current', String(marker.dataset.turnId === id));
    });
  }

  /**
   * Update the jump-to-latest control after scrolling.
   *
   * Inputs: Reads the analysis chat scroll position.
   * Returns: Nothing; toggles the control and clears historical selection at the bottom.
   * Details: Registered as a passive scroll handler and called after rendering.
   */
  function chatScroll() {
    const chat = $('analysis-chat');
    const atBottom = chat.scrollHeight - chat.clientHeight - chat.scrollTop < 40;
    $('analysis-latest').hidden = atBottom;
    if (atBottom && activeTurn) selectTurn('');
  }

  /**
   * Scroll to a saved turn without moving the whole page.
   *
   * Inputs: turn: record containing an ID.
   * Returns: Nothing; selects the turn and scrolls the chat container.
   * Details: Used by navigator buttons and keyboard history; respects reduced-motion
   *     settings.
   */
  function jumpToTurn(turn) {
    const chat = $('analysis-chat'), anchor = $(`analysis-turn-${turn.id}`);
    if (!anchor) return;
    selectTurn(turn.id);
    const top = chat.scrollTop + anchor.getBoundingClientRect().top - chat.getBoundingClientRect().top - 12;
    chat.scrollTo({top, behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'});
  }

  /**
   * Navigate to the previous turn group with Arrow Up.
   *
   * Inputs: event: keyboard event; reads snapshot turns and active selection.
   * Returns: Nothing; prevents default only when a previous group can be selected.
   * Details: Used from an empty composer or the focused chat container; modified key
   *     combinations remain untouched.
   */
  function previousParent(event) {
    if (event.key !== 'ArrowUp' || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    const turns = snapshot?.turns || [];
    const parents = turns.filter(turn => turn.id === turn.parent_id);
    const group = turns.find(turn => turn.id === activeTurn)?.parent_id;
    const index = group ? parents.findIndex(turn => turn.id === group) : parents.length - 1;
    if (index <= 0) return;
    event.preventDefault();
    jumpToTurn(parents[index - 1]);
  }

  /**
   * Render the grouped turn navigator and hover preview.
   *
   * Inputs: turns: chronological navigation/parent records.
   * Returns: Nothing; rebuilds markers only when the turn signature changes.
   * Details: Parent groups receive numbered markers and continuations receive dots; marker
   *     supplies hover/focus preview behavior.
   */
  function renderTurns(turns) {
    const key = JSON.stringify(turns);
    if (key === turnsKey) return;
    turnsKey = key;
    $('analysis-turns').hidden = !turns.length;
    const list = $('analysis-turn-list'), preview = $('analysis-turn-preview');
    list.replaceChildren();
    preview.hidden = true;
    /**
     * Create a turn-navigation button and its preview handlers.
     *
     * Inputs: turn: navigation metadata; label: marker text; kind: CSS classes.
     * Returns: Unattached button.
     * Details: Nested in renderTurns; click jumps to the turn, while hover/focus shows its
     *     bounded header/summary.
     */
    function marker(turn, label, kind) {
      const control = button(label, () => jumpToTurn(turn), kind);
      const heading = formattedMessage(turn.navigation.header, 'analysis-turn-heading');
      control.dataset.turnId = turn.id;
      control.setAttribute('aria-label', heading.textContent);
      control.setAttribute('aria-current', String(turn.id === activeTurn));
      /**
       * Reveal the current turn marker's text preview.
       *
       * Inputs: Captures preview container, heading and turn navigation summary.
       * Returns: Nothing; replaces preview content and reveals it.
       * Details: Shared hover/focus callback inside renderTurns.marker.
       */
      const show = () => {
        preview.replaceChildren(heading, formattedMessage(turn.navigation.summary, 'analysis-turn-summary'));
        preview.hidden = false;
      };
      control.onmouseenter = show;
      control.onfocus = show;
      control.onmouseleave = () => { if (document.activeElement !== control) preview.hidden = true; };
      control.onblur = () => { preview.hidden = true; };
      control.onkeydown = event => { if (event.key === 'Escape') preview.hidden = true; };
      return control;
    }
    let group, parentId, index = 0;
    for (const turn of turns) {
      if (turn.parent_id !== parentId) {
        parentId = turn.parent_id;
        group = el('div', undefined, 'analysis-turn-markers');
        group.dataset.parentId = parentId;
        group.append(marker(turn, String(++index), 'analysis-turn-marker'));
        list.append(group);
      } else {
        group.append(marker(turn, '•', 'analysis-turn-marker analysis-turn-child'));
      }
    }
  }

  /**
   * Render analysis messages, results and turn groups.
   *
   * When: Polling, chat switching, feedback or submission changes the snapshot.
   * Inputs: Reads snapshot, chatId, submission/stop state and render signatures.
   * Returns: Nothing; updates composer controls, logs/transcript links and chat DOM.
   * Flow: Groups progress and related turns, attaches tables/methods/ratings and preserves
   *     scroll unless a new answer should be shown. Delegates navigation state to
   *     renderTurns/selectTurn.
   */
  function renderChat() {
    const messages = snapshot?.messages || [];
    const turns = snapshot?.turns || [];
    const active = ['running', 'stopping'].includes(snapshot?.status);
    const stopping = snapshot?.status === 'stopping' || Boolean(chatId && stopRequest === chatId);
    const running = Boolean(chatId) && (active || submittingQuestion || !snapshot);
    $('analysis-form').hidden = !chatId;
    $('analysis-send').disabled = !chatId || running;
    $('analysis-send').hidden = active;
    $('analysis-stop').hidden = !active;
    $('analysis-stop').disabled = stopping;
    $('analysis-stop').setAttribute('aria-label', stopping ? 'Stopping analysis' : 'Stop analysis');
    $('analysis-activity').hidden = !running;
    $('analysis-stage').textContent = snapshot?.activity || 'Understanding your question';
    const log = $('analysis-log');
    log.hidden = !snapshot?.log_path;
    if (!log.hidden) log.href = endpoint(`download/${snapshot.log_path}`);
    const transcript = $('analysis-transcript');
    transcript.hidden = !snapshot?.transcript_path;
    if (!transcript.hidden) transcript.href = endpoint(`download/${snapshot.transcript_path}`);
    renderTurns(turns);
    const key = JSON.stringify([chatId, messages, turns.map(turn => [turn.parent_id, turn.feedback])]);
    if (key === chatKey) return;
    chatKey = key;
    const chat = $('analysis-chat');
    const atBottom = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 100;
    const latest = messages.findLast(message => message.kind !== 'thinking')?.id || '';
    const newMessage = latest !== latestMessage;
    latestMessage = latest;
    const oldScroll = chat.scrollTop;
    chat.replaceChildren();
    if (!chatId) {
      const empty = el('div', undefined, 'empty-chat');
      empty.append(el('p', 'Start a new chat to ask about this project.'));
      chat.append(empty);
    }
    const byId = new Map(turns.map(turn => [turn.id, turn]));
    let thinking = null, group, parentId, turnId, target = chat, palette = 0;
    for (const message of messages) {
      const turn = byId.get(message.turn_id);
      if (turn && turn.parent_id !== parentId) {
        parentId = turn.parent_id;
        group = el('section', undefined, 'analysis-turn-group');
        group.dataset.tone = String(palette++ % 2);
        chat.append(group);
      }
      if (turn?.id !== turnId) {
        turnId = turn?.id;
        target = turn ? el('div', undefined, 'analysis-turn-content') : chat;
        if (turn) {
          target.id = `analysis-turn-${turn.id}`;
          group.append(target);
        }
        thinking = null;
      }
      if (message.kind === 'thinking') {
        if (!thinking) {
          const group = disclosure(`analysis-thinking-${message.id}`, 'Thinking', 'thinking-group analysis-thinking');
          group.querySelector('summary').prepend(el('span', undefined, 'thinking-orb'));
          thinking = el('div', undefined, 'thinking-content');
          group.append(thinking);
          target.append(group);
        }
        thinking.append(thinkingStep(message));
        continue;
      }
      thinking = null;
      if (message.kind === 'scope') {
        target.append(scopeNotice(message));
        continue;
      }
      const user = message.agent === 'You';
      const wrap = el('article', undefined, `message ${user ? 'user' : ''} ${message.kind === 'error' ? 'error' : ''}`);
      wrap.append(el('span', user ? 'You' : 'Analysis', 'author'));
      const bubble = user ? el('div', message.text, 'bubble') : formattedMessage(message.text, 'bubble');
      if (!user) bubble.querySelectorAll('table').forEach(table => markSmallSamples(table));
      wrap.append(bubble);
      for (const result of message.details?.tables || []) wrap.append(renderTable(result, message.details?.decimal_places));
      const method = renderMethod(message);
      if (method) wrap.append(method);
      if (turn && message.agent === 'analysis_orchestrator') wrap.append(renderFeedback(turn));
      target.append(wrap);
    }
    selectTurn(newMessage ? '' : activeTurn);
    chat.scrollTop = atBottom || newMessage ? chat.scrollHeight : oldScroll;
    chatScroll();
  }

  /**
   * Build the collapsible notice shown when the folder scope changes between questions.
   *
   * Inputs: message: scope message with details.project_name and details.tree folder records.
   * Returns: Unattached disclosure listing the project and its folders.
   * Details: Selected items get a soft highlight; the project itself is highlighted when every
   *     folder is selected.
   */
  function scopeNotice(message) {
    const {project_name: projectName, tree = []} = message.details || {};
    const notice = disclosure(`analysis-scope-${message.id}`, message.text || 'Updated folder scope', 'scope-notice');
    const item = (label, selected) => el('span', label, `scope-notice-item${selected ? ' is-selected' : ''}`);
    const root = el('ul', undefined, 'scope-notice-tree');
    const project = el('li');
    project.append(item(projectName || 'Entire project', tree.length > 0 && tree.every(entry => entry.selected)));
    const folders = el('ul');
    for (const entry of tree) {
      const row = el('li');
      row.append(item(entry.name, entry.selected));
      folders.append(row);
    }
    project.append(folders);
    root.append(project);
    notice.append(root);
    return notice;
  }

  /**
   * Render the saved-chat list and deletion controls.
   *
   * Inputs: Reads chats and selected chatId.
   * Returns: Nothing; rebuilds the list when its signature changes.
   * Details: Open buttons call activateChat; delete buttons open confirmation without
   *     deleting immediately.
   */
  function renderChats() {
    const key = JSON.stringify([chatId, chats]);
    if (key === chatListKey) return;
    chatListKey = key;
    const list = $('analysis-chat-list');
    list.replaceChildren();
    for (const chat of chats || []) {
      const item = el('li', undefined, 'analysis-chat-item');
      const open = button(chat.title, () => activateChat(chat.id), 'analysis-chat-choice');
      if (chat.id === chatId) open.setAttribute('aria-current', 'true');
      open.title = chat.title;
      const remove = button('×', () => {
        deletingChat = chat.id;
        $('analysis-delete-description').textContent = `Delete “${chat.title}” and its saved turns and run files?`;
        $('analysis-delete-error').textContent = '';
        $('analysis-delete-dialog').showModal();
      }, 'icon-button analysis-chat-delete');
      remove.setAttribute('aria-label', `Delete ${chat.title}`);
      item.append(open, remove);
      list.append(item);
    }
  }

  /**
   * Switch analysis chats and clear the previous chat's display state.
   *
   * Inputs: id: selected chat ID.
   * Returns: Nothing; resets snapshot/navigation/composer and starts refresh.
   * Details: Ignores redundant switches and waits during question submission so another chat
   *     cannot receive stale local state.
   */
  function activateChat(id) {
    if (id === chatId || submittingQuestion) return;
    chatId = id;
    snapshot = null;
    checking = null;
    greetingRequested = false;
    activeTurn = latestMessage = '';
    $('analysis-question').value = '';
    $('analysis-error').textContent = '';
    renderChats();
    renderChat();
    refresh();
  }

  /**
   * Poll analysis state and initialize an empty chat greeting.
   *
   * When: The visible analysis workspace requests an update.
   * Inputs: Reads project/chat IDs and the request guard.
   * Returns: Promise with no value; updates chat list, snapshot, scope and rendered panels.
   * Flow: Loads chats once, fetches the active chat, and posts a greeting only for an empty
   *     idle chat. currentRequest prevents stale responses after selection changes.
   */
  async function refresh() {
    const id = projectId;
    if (!id || $('analyse-workspace').hidden) return;
    renderScope();
    loadTree(id);
    if (checking) return;
    const check = {};
    checking = check;
    /**
     * Check whether this asynchronous refresh still owns the view.
     *
     * Inputs: Captures request marker and project ID; reads checking/projectId.
     * Returns: Boolean.
     * Details: Private refresh guard; activateChat/reset invalidate the marker before older
     *     responses arrive.
     */
    const currentRequest = () => checking === check && projectId === id;
    try {
      if (!chats) {
        const saved = await api(`/api/projects/${id}/analysis/chats`);
        if (!currentRequest()) return;
        chats = saved;
        $('analysis-chats-error').textContent = '';
        chatId = saved[0]?.id || '';
        renderChats();
        renderChat();
      }
      if (!chatId) return;
      const url = `/api/projects/${id}/analysis/chats/${chatId}`;
      const current = await api(url);
      if (!currentRequest()) return;
      snapshot = current;
      chats.find(chat => chat.id === chatId).title = current.title;
      renderChats();
      renderChat();
      if (!current.messages.length && current.status === 'idle' && !greetingRequested) {
        greetingRequested = true;
        await api(url, {folder});
        if (currentRequest()) {
          snapshot = {...snapshot, status:'running'};
          renderChat();
        }
      }
    } catch (error) {
      if (currentRequest()) $(chats ? 'analysis-error' : 'analysis-chats-error').textContent = error.message;
    } finally {
      if (checking === check) checking = null;
    }
  }

  /**
   * Create and open a new analysis chat.
   *
   * When: The New chat button is clicked.
   * Inputs: Reads projectId and submission state.
   * Returns: Promise with no value; adds/activates the new chat or shows an error.
   * Flow: Disables duplicate creation while posting and ignores results if the project
   *     changed.
   */
  $('analysis-new-chat').onclick = async () => {
    if (submittingQuestion) return;
    const id = projectId;
    $('analysis-new-chat').disabled = true;
    $('analysis-chats-error').textContent = '';
    try {
      const chat = await api(endpoint('analysis/chats'), {});
      if (projectId !== id) return;
      const saved = chats || await api(`/api/projects/${id}/analysis/chats`);
      if (projectId !== id) return;
      chats = [chat, ...saved.filter(item => item.id !== chat.id)];
      folder = ''; treeKey = '';
      activateChat(chat.id);
    } catch (error) {
      if (projectId === id) $('analysis-chats-error').textContent = error.message;
    } finally {
      $('analysis-new-chat').disabled = false;
    }
  };

  /**
   * Delete the chat selected in the confirmation dialog.
   *
   * When: The chat deletion form is submitted.
   * Inputs: event: form submission; reads deletingChat and projectId.
   * Returns: Promise with no value; removes the chat and selects a remaining one when
   *     needed.
   * Flow: Calls the deletion endpoint, retains inline errors and restores the submit button.
   */
  $('analysis-delete-form').onsubmit = async event => {
    event.preventDefault();
    const id = projectId, target = deletingChat;
    $('analysis-delete-submit').disabled = true;
    try {
      await api(endpoint(`analysis/chats/${target}/delete`), {});
      if (projectId !== id) return;
      $('analysis-delete-dialog').close();
      chats = chats.filter(chat => chat.id !== target);
      if (target === chatId) activateChat(chats[0]?.id || '');
      renderChats();
    } catch (error) {
      if (projectId === id) $('analysis-delete-error').textContent = error.message;
    } finally {
      $('analysis-delete-submit').disabled = false;
    }
  };

  /**
   * Submit a question in the selected chat and scope.
   *
   * When: The analysis composer is submitted.
   * Inputs: event: form submission; reads text, chatId and folder.
   * Returns: Promise with no value; marks the chat running and refreshes, or keeps an error.
   * Flow: Blocks overlapping submissions, moves to the latest turn, and clears text only
   *     after the server accepts the request.
   */
  $('analysis-form').onsubmit = async event => {
    event.preventDefault();
    const input = $('analysis-question'), id = projectId, text = input.value.trim();
    if (!chatId || !text || submittingQuestion || ['running', 'stopping'].includes(snapshot?.status)) return;
    $('analysis-error').textContent = '';
    selectTurn('');
    $('analysis-chat').scrollTop = $('analysis-chat').scrollHeight;
    submittingQuestion = true;
    renderChat();
    try {
      await api(endpoint(`analysis/chats/${chatId}`), {folder, text});
      if (projectId === id) {
        input.value = '';
        snapshot = {...snapshot, status:'running'};
        await refresh();
      }
    } catch (error) {
      if (projectId === id) $('analysis-error').textContent = error.message;
    } finally {
      submittingQuestion = false;
      if (projectId === id) renderChat();
    }
  };

  /**
   * Request cancellation while keeping the correct chat's UI current.
   *
   * When: The analysis Stop button is clicked.
   * Inputs: Captures projectId/chatId and sets stopRequest.
   * Returns: Promise with no value; refreshes stopping/idle state.
   * Flow: Uses currentChat after awaited calls and clears only its own pending stop marker.
   */
  $('analysis-stop').onclick = async () => {
    const id = projectId, target = chatId;
    /**
     * Check that a stop response still belongs to the displayed chat.
     *
     * Inputs: Captures the stop request's project and chat IDs.
     * Returns: Boolean.
     * Details: Used after awaited stop/refresh calls so cancellation feedback cannot affect
     *     another chat.
     */
    const currentChat = () => projectId === id && chatId === target;
    stopRequest = target;
    $('analysis-error').textContent = '';
    renderChat();
    try {
      const {stopping} = await api(endpoint(`analysis/chats/${target}/stop`), {});
      if (currentChat()) {
        if (stopping && snapshot?.status === 'running') snapshot = {...snapshot, status:'stopping'};
        await refresh();
      }
    } catch (error) {
      if (currentChat()) $('analysis-error').textContent = error.message;
    } finally {
      if (stopRequest === target) stopRequest = '';
      if (currentChat()) renderChat();
    }
  };

  /**
   * Submit a thumbs-down rating with its required explanation.
   *
   * When: The feedback dialog is submitted.
   * Inputs: event: form submission; reads comment and feedbackTurn.
   * Returns: Promise with no value; closes on success or displays a validation/request
   *     error.
   * Flow: Requires nonempty text, then calls saveFeedback and restores the submit button.
   */
  $('analysis-feedback-form').onsubmit = async event => {
    event.preventDefault();
    const comment = $('analysis-feedback-comment').value.trim();
    if (!comment) {
      $('analysis-feedback-error').textContent = 'Please tell us what went wrong.';
      return;
    }
    $('analysis-feedback-submit').disabled = true;
    try {
      await saveFeedback(feedbackTurn, 'down', comment);
      $('analysis-feedback-dialog').close();
    } catch (error) {
      $('analysis-feedback-error').textContent = error.message;
    } finally {
      $('analysis-feedback-submit').disabled = false;
    }
  };

  $('analysis-question').addEventListener('keydown', event => {
    if (event.isComposing) return;
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      if (!$('analysis-send').disabled) $('analysis-form').requestSubmit();
    } else if (!event.currentTarget.value.trim()) {
      previousParent(event);
    }
  });
  $('analysis-chat').addEventListener('keydown', event => {
    if (event.target === event.currentTarget) previousParent(event);
  });

  $('analysis-chat').addEventListener('scroll', chatScroll, {passive: true});
  $('analysis-latest').onclick = () => {
    const chat = $('analysis-chat');
    selectTurn('');
    chat.scrollTo({top: chat.scrollHeight, behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'});
  };

  window.analysisView = {
    refresh,
    /**
     * Clear all analysis UI state when the project changes.
     *
     * Inputs: No arguments; uses private analysis view state and page elements.
     * Returns: Nothing; clears selection, cached responses, expansion state and
     *     composer/errors.
     * Details: Exposed as window.analysisView.reset for the project workspace in app.js.
     */
    reset() {
      folder = ''; snapshot = null; treeKey = ''; chatKey = ''; greetingRequested = false;
      chats = null; chatId = ''; chatListKey = ''; checking = null;
      stopRequest = '';
      turnsKey = ''; activeTurn = ''; latestMessage = '';
      $('analysis-turn-preview').hidden = true;
      requestedTree = ''; treeDetails = null; folderOpen.clear();
      $('analysis-question').value = '';
      $('analysis-error').textContent = '';
      $('analysis-chats-error').textContent = '';
    }
  };
})();
