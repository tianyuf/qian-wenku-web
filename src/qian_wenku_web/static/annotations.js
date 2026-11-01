/* Entry annotations: highlights anchored to text positions, a selection
 * popover, inline editing, and two-way linking between text and notes.
 *
 * Text model: the entry body is a sequence of <p> elements. Positions are
 * character offsets into the paragraphs' text joined with "\n". Highlights
 * are stored as (quote, anchor_start, anchor_end, prefix, suffix); they are
 * re-found by position first, then by quote with context, then by a
 * whitespace-insensitive match, and reported as orphaned otherwise.
 */
(function () {
    'use strict';

    var CONTEXT = 32;
    var MAX_QUOTE = 500;

    // ------------------------------------------------------------------
    // Text model
    // ------------------------------------------------------------------

    function textMap(contentEl) {
        var segments = [];
        var text = '';
        var paras = Array.prototype.slice.call(contentEl.querySelectorAll('p'));
        paras.forEach(function (p, index) {
            if (index > 0) text += '\n';
            var walker = document.createTreeWalker(p, NodeFilter.SHOW_TEXT, null);
            var node;
            while ((node = walker.nextNode())) {
                segments.push({
                    node: node,
                    start: text.length,
                    end: text.length + node.nodeValue.length
                });
                text += node.nodeValue;
            }
        });
        return { text: text, segments: segments, paras: paras, root: contentEl };
    }

    function positionOf(map, container, offset) {
        var segments = map.segments;
        var i;
        if (container.nodeType === 3) {
            for (i = 0; i < segments.length; i++) {
                if (segments[i].node === container) return segments[i].start + offset;
            }
        }
        // Not a mapped text node: resolve to the nearest mapped position.
        var probe = container.nodeType === 3 ? container : container.childNodes[offset] || null;
        if (!probe) {
            for (i = segments.length - 1; i >= 0; i--) {
                if (container.contains(segments[i].node)) return segments[i].end;
            }
            return map.text.length;
        }
        for (i = 0; i < segments.length; i++) {
            var node = segments[i].node;
            if (probe === node || probe.contains(node) ||
                (probe.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_FOLLOWING)) {
                return segments[i].start;
            }
        }
        return map.text.length;
    }

    function selectionAnchor(contentEl) {
        var sel = window.getSelection();
        if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return null;
        var range = sel.getRangeAt(0);
        if (!contentEl.contains(range.startContainer) || !contentEl.contains(range.endContainer)) {
            return null;
        }
        var map = textMap(contentEl);
        var start = positionOf(map, range.startContainer, range.startOffset);
        var end = positionOf(map, range.endContainer, range.endOffset);
        if (start < 0 || end <= start) return null;
        var slice = map.text.slice(start, end);
        start += slice.length - slice.replace(/^\s+/, '').length;
        end -= slice.length - slice.replace(/\s+$/, '').length;
        if (end <= start) return null;
        return {
            start: start,
            end: end,
            tooLong: end - start > MAX_QUOTE,
            quote: map.text.slice(start, end),
            prefix: map.text.slice(Math.max(0, start - CONTEXT), start),
            suffix: map.text.slice(end, end + CONTEXT),
            range: range
        };
    }

    // ------------------------------------------------------------------
    // Re-anchoring
    // ------------------------------------------------------------------

    function commonSuffixLength(a, b) {
        var n = 0;
        while (n < a.length && n < b.length && a[a.length - 1 - n] === b[b.length - 1 - n]) n++;
        return n;
    }

    function commonPrefixLength(a, b) {
        var n = 0;
        while (n < a.length && n < b.length && a[n] === b[n]) n++;
        return n;
    }

    function fuzzyLocate(text, quote) {
        var compactQuote = quote.replace(/\s+/g, '');
        if (!compactQuote) return null;
        var compact = '';
        var back = [];
        for (var i = 0; i < text.length; i++) {
            if (!/\s/.test(text[i])) { compact += text[i]; back.push(i); }
        }
        var at = compact.indexOf(compactQuote);
        if (at === -1) return null;
        return { start: back[at], end: back[at + compactQuote.length - 1] + 1, fuzzy: true };
    }

    function locate(map, note) {
        var quote = note.quote;
        if (!quote) return null;
        var text = map.text;
        var hasAnchor = note.anchor_start != null && note.anchor_end != null;
        if (hasAnchor && text.slice(note.anchor_start, note.anchor_end) === quote) {
            return { start: note.anchor_start, end: note.anchor_end };
        }
        var candidates = [];
        var idx = text.indexOf(quote);
        while (idx !== -1 && candidates.length < 200) {
            candidates.push(idx);
            idx = text.indexOf(quote, idx + 1);
        }
        if (!candidates.length) return fuzzyLocate(text, quote);
        if (candidates.length === 1) return { start: candidates[0], end: candidates[0] + quote.length };
        var best = candidates[0];
        var bestScore = -Infinity;
        candidates.forEach(function (c) {
            var score = 0;
            if (note.prefix) {
                score += commonSuffixLength(text.slice(Math.max(0, c - note.prefix.length), c), note.prefix);
            }
            if (note.suffix) {
                score += commonPrefixLength(text.slice(c + quote.length, c + quote.length + note.suffix.length), note.suffix);
            }
            score = score * 100000 - (hasAnchor ? Math.abs(c - note.anchor_start) : 0);
            if (score > bestScore) { bestScore = score; best = c; }
        });
        return { start: best, end: best + quote.length };
    }

    // ------------------------------------------------------------------
    // Highlight rendering
    // ------------------------------------------------------------------

    function clearHighlights(contentEl) {
        Array.prototype.slice.call(contentEl.querySelectorAll('mark.entry-highlight')).forEach(function (mark) {
            var parent = mark.parentNode;
            while (mark.firstChild) parent.insertBefore(mark.firstChild, mark);
            parent.removeChild(mark);
        });
        Array.prototype.slice.call(contentEl.querySelectorAll('p')).forEach(function (p) { p.normalize(); });
    }

    function wrapRange(contentEl, start, end) {
        var map = textMap(contentEl);
        var marks = [];
        map.segments.forEach(function (seg) {
            var s = Math.max(start, seg.start);
            var e = Math.min(end, seg.end);
            if (e <= s) return;
            var node = seg.node;
            var localStart = s - seg.start;
            var localEnd = e - seg.start;
            if (localEnd < node.nodeValue.length) node.splitText(localEnd);
            var target = localStart > 0 ? node.splitText(localStart) : node;
            if (!target.nodeValue.trim()) return;
            var mark = document.createElement('mark');
            mark.className = 'entry-highlight';
            target.parentNode.insertBefore(mark, target);
            mark.appendChild(target);
            marks.push(mark);
        });
        return marks;
    }

    // Places every note's highlight; returns {noteId: {start, end} | null}.
    function applyHighlights(contentEl, notes, options) {
        options = options || {};
        clearHighlights(contentEl);
        var map = textMap(contentEl);
        var placed = {};
        notes.forEach(function (note) {
            if (!note.quote) return;
            placed[note.id] = locate(map, note);
        });
        var ordered = notes.filter(function (n) { return placed[n.id]; }).sort(function (a, b) {
            return placed[a.id].start - placed[b.id].start || (placed[a.id].end - placed[b.id].end);
        });
        ordered.forEach(function (note) {
            var loc = placed[note.id];
            wrapRange(contentEl, loc.start, loc.end).forEach(function (mark) {
                mark.setAttribute('data-note-id', String(note.id));
                if (note.tag) mark.setAttribute('data-tag', note.tag);
                mark.setAttribute('role', 'button');
                mark.setAttribute('tabindex', '0');
                var preview = (note.body || '').slice(0, 80);
                mark.setAttribute('aria-label', '批注' + (preview ? '：' + preview : ''));
                var go = function () {
                    if (options.href) {
                        window.location.href = options.href + '#note-' + note.id;
                    } else if (options.onClick) {
                        options.onClick(note.id);
                    }
                };
                mark.addEventListener('click', go);
                mark.addEventListener('keydown', function (e) {
                    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); }
                });
            });
        });
        return placed;
    }

    window.wenkuAnnotations = { applyHighlights: applyHighlights, textMap: textMap };

    // ------------------------------------------------------------------
    // Interactive panel (signed-in readers only)
    // ------------------------------------------------------------------

    var dataEl = document.getElementById('notes-data');
    var panel = document.getElementById('entry-notes');
    var contentEl = document.getElementById('entry-content');
    if (!dataEl || !panel || !contentEl) return;

    var config = JSON.parse(dataEl.textContent);
    var entryId = config.entryId;
    var csrfToken = config.csrfToken;
    var TAGS = config.tags || {};
    var notes = config.notes || [];
    var placements = {};

    var list = document.getElementById('entry-notes-list');
    var hint = document.getElementById('note-hint');
    var bodyInput = document.getElementById('note-body');
    var formTags = document.getElementById('note-form-tags');
    var popover = document.getElementById('note-popover');
    var popQuote = document.getElementById('note-popover-quote');
    var popBody = document.getElementById('note-popover-body');
    var popTags = document.getElementById('note-popover-tags');
    var popHint = document.getElementById('note-popover-hint');
    var toast = document.getElementById('note-toast');

    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text != null) node.textContent = text;
        return node;
    }

    function setHint(text) { if (hint) hint.textContent = text || ''; }

    function formatTimestamp(timestamp) {
        return new Date(Number(timestamp) * 1000).toLocaleString('zh-CN', {
            year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'
        });
    }

    function request(method, url, fields) {
        var options = { method: method, headers: { 'X-CSRF-Token': csrfToken } };
        if (fields) {
            options.headers['Content-Type'] = 'application/x-www-form-urlencoded';
            options.body = new URLSearchParams(fields).toString();
        }
        return fetch(url, options).then(function (r) {
            if (r.status === 401) {
                window.location.href = '/login?next=' + encodeURIComponent(window.location.pathname);
                return null;
            }
            return r.json();
        });
    }

    // --- tag chips -----------------------------------------------------

    function renderTagChips(container, selected, onChange) {
        container.innerHTML = '';
        container.setAttribute('data-selected', selected || '');
        Object.keys(TAGS).forEach(function (key) {
            var chip = el('button', 'note-tag-chip' + (selected === key ? ' is-selected' : ''), TAGS[key]);
            chip.type = 'button';
            chip.setAttribute('data-tag', key);
            chip.setAttribute('aria-pressed', selected === key ? 'true' : 'false');
            chip.addEventListener('click', function () {
                var next = container.getAttribute('data-selected') === key ? '' : key;
                renderTagChips(container, next, onChange);
                if (onChange) onChange(next);
            });
            container.appendChild(chip);
        });
    }

    function selectedTag(container) { return container ? container.getAttribute('data-selected') || '' : ''; }

    function tagChip(tag) {
        return tag && TAGS[tag] ? el('span', 'note-tag note-tag-' + tag, TAGS[tag]) : null;
    }

    // --- notes list -----------------------------------------------------

    function sortedNotes() {
        return notes.slice().sort(function (a, b) {
            var pa = placements[a.id], pb = placements[b.id];
            if (pa && pb) return pa.start - pb.start || a.created_at - b.created_at;
            if (pa) return -1;
            if (pb) return 1;
            return a.created_at - b.created_at || a.id - b.id;
        });
    }

    function flash(node) {
        if (!node) return;
        node.classList.remove('is-flashing');
        void node.offsetWidth;
        node.classList.add('is-flashing');
        setTimeout(function () { node.classList.remove('is-flashing'); }, 1600);
    }

    function focusNote(noteId) {
        var node = document.getElementById('note-' + noteId);
        if (!node) return;
        node.scrollIntoView({ behavior: 'smooth', block: 'center' });
        flash(node);
    }

    function focusHighlight(noteId) {
        var marks = contentEl.querySelectorAll('mark.entry-highlight[data-note-id="' + noteId + '"]');
        if (!marks.length) return false;
        marks[0].scrollIntoView({ behavior: 'smooth', block: 'center' });
        Array.prototype.forEach.call(marks, flash);
        return true;
    }

    function renderNote(note) {
        var box = el('div', 'entry-note');
        box.id = 'note-' + note.id;
        box.setAttribute('data-note-id', String(note.id));
        if (note.tag) box.setAttribute('data-tag', note.tag);

        var chip = tagChip(note.tag);
        if (chip) box.appendChild(chip);

        if (note.quote) {
            var quote = el('blockquote', 'entry-note-quote', '「' + note.quote + '」');
            if (note.quote && placements[note.id] === null) {
                quote.classList.add('is-orphan');
                quote.appendChild(el('span', 'entry-note-orphan', '原文中未找到此段'));
            } else {
                quote.setAttribute('role', 'button');
                quote.setAttribute('tabindex', '0');
                quote.title = '在正文中定位';
                var jump = function () { focusHighlight(note.id); };
                quote.addEventListener('click', jump);
                quote.addEventListener('keydown', function (e) {
                    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); jump(); }
                });
            }
            box.appendChild(quote);
        }

        if (note.body) box.appendChild(el('div', 'entry-note-body', note.body));

        var meta = el('div', 'entry-note-meta');
        var when = formatTimestamp(note.created_at) +
            (note.updated_at !== note.created_at ? ' · 编辑于 ' + formatTimestamp(note.updated_at) : '');
        meta.appendChild(el('span', null, when));
        var edit = el('button', 'btn btn-sm btn-link entry-note-edit', '编辑');
        edit.type = 'button';
        edit.addEventListener('click', function () { startEdit(note, box); });
        var del = el('button', 'btn btn-sm btn-link entry-note-delete', '删除');
        del.type = 'button';
        del.addEventListener('click', function () { deleteNote(note); });
        meta.appendChild(edit);
        meta.appendChild(del);
        box.appendChild(meta);
        return box;
    }

    function render() {
        placements = applyHighlights(contentEl, notes, { onClick: focusNote });
        list.innerHTML = '';
        if (!notes.length) {
            list.appendChild(el('p', 'text-muted small mb-0', '暂无批注。选中正文文字即可高亮或批注，或在下方直接写评论。'));
            return;
        }
        sortedNotes().forEach(function (note) { list.appendChild(renderNote(note)); });
    }

    function refresh() {
        return request('GET', '/api/notes?entry_id=' + entryId).then(function (data) {
            if (!data) return;
            notes = data.notes || [];
            render();
        });
    }

    // --- inline editing -------------------------------------------------

    function startEdit(note, box) {
        var editor = el('div', 'entry-note-editor');
        var tags = el('div', 'note-tag-chips');
        renderTagChips(tags, note.tag);
        var area = el('textarea', 'form-control form-control-sm');
        area.rows = 3;
        area.maxLength = 5000;
        area.value = note.body || '';
        area.placeholder = note.quote ? '批注（留空则仅保留高亮）' : '批注内容';
        var actions = el('div', 'd-flex align-items-center gap-2 mt-2');
        var save = el('button', 'btn btn-sm btn-primary', '保存');
        save.type = 'button';
        var cancel = el('button', 'btn btn-sm btn-outline-secondary', '取消');
        cancel.type = 'button';
        var status = el('span', 'text-muted small');
        actions.appendChild(save);
        actions.appendChild(cancel);
        actions.appendChild(status);
        editor.appendChild(tags);
        editor.appendChild(area);
        editor.appendChild(actions);

        var original = box;
        var replacement = el('div', 'entry-note is-editing');
        replacement.id = box.id;
        var chip = tagChip(note.tag);
        if (note.quote) replacement.appendChild(el('blockquote', 'entry-note-quote', '「' + note.quote + '」'));
        replacement.appendChild(editor);
        original.parentNode.replaceChild(replacement, original);
        area.focus();

        function finish() { replacement.parentNode.replaceChild(renderNote(note), replacement); }
        function submit() {
            var body = area.value.trim();
            if (!body && !note.quote) { status.textContent = '请输入批注内容。'; return; }
            save.disabled = true;
            request('PUT', '/api/notes/' + note.id, { body: body, tag: selectedTag(tags) }).then(function (data) {
                if (!data) return;
                if (data.error) { status.textContent = data.error; save.disabled = false; return; }
                refresh().then(function () { flash(document.getElementById('note-' + note.id)); });
            }).catch(function () { status.textContent = '保存失败，请稍后重试。'; save.disabled = false; });
        }
        save.addEventListener('click', submit);
        cancel.addEventListener('click', finish);
        area.addEventListener('keydown', function (e) {
            if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); submit(); }
            if (e.key === 'Escape') { e.preventDefault(); finish(); }
        });
        void chip;
    }

    // --- delete with undo -----------------------------------------------

    var toastTimer = null;

    function showToast(message, actionLabel, onAction) {
        if (!toast) return;
        toast.innerHTML = '';
        toast.appendChild(el('span', null, message));
        if (actionLabel) {
            var action = el('button', 'btn btn-sm btn-link', actionLabel);
            action.type = 'button';
            action.addEventListener('click', function () { hideToast(); onAction(); });
            toast.appendChild(action);
        }
        toast.hidden = false;
        clearTimeout(toastTimer);
        toastTimer = setTimeout(hideToast, 7000);
    }

    function hideToast() { if (toast) toast.hidden = true; }

    function deleteNote(note) {
        notes = notes.filter(function (n) { return n.id !== note.id; });
        render();
        request('DELETE', '/api/notes/' + note.id).then(function (data) {
            if (!data) return;
            if (data.error) { setHint(data.error); refresh(); return; }
            showToast('已删除批注。', '撤销', function () {
                var fields = { entry_id: entryId, body: note.body || '', quote: note.quote || '', tag: note.tag || '' };
                if (note.quote && note.anchor_start != null) {
                    fields.anchor_start = note.anchor_start;
                    fields.anchor_end = note.anchor_end;
                    fields.prefix = note.prefix || '';
                    fields.suffix = note.suffix || '';
                }
                request('POST', '/api/notes', fields).then(function (created) {
                    if (!created) return;
                    if (created.error) { setHint(created.error); return; }
                    refresh().then(function () { flash(document.getElementById('note-' + created.note_id)); });
                });
            });
        }).catch(function () { setHint('删除批注失败，请稍后重试。'); refresh(); });
    }

    // --- bottom form: comment without a highlight -----------------------

    renderTagChips(formTags, '');

    function submitGeneral() {
        var body = bodyInput.value.trim();
        if (!body) { setHint('请输入批注内容。'); return; }
        request('POST', '/api/notes', { entry_id: entryId, body: body, quote: '', tag: selectedTag(formTags) })
            .then(function (data) {
                if (!data) return;
                if (data.error) { setHint(data.error); return; }
                bodyInput.value = '';
                renderTagChips(formTags, '');
                setHint('已添加。');
                refresh().then(function () { flash(document.getElementById('note-' + data.note_id)); });
            }).catch(function () { setHint('保存批注失败，请稍后重试。'); });
    }

    document.getElementById('note-submit').addEventListener('click', submitGeneral);
    bodyInput.addEventListener('keydown', function (e) {
        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); submitGeneral(); }
    });

    // --- selection popover -----------------------------------------------

    var pending = null;      // current selection anchor
    var selectionTimer = null;

    function positionPopover() {
        if (!pending || popover.hidden) return;
        var rect = pending.range.getBoundingClientRect();
        var width = popover.offsetWidth;
        var height = popover.offsetHeight;
        var left = Math.min(Math.max(8, rect.left), window.innerWidth - width - 8);
        var top = rect.bottom + 8;
        if (top + height > window.innerHeight - 8) top = Math.max(8, rect.top - height - 8);
        popover.style.left = left + 'px';
        popover.style.top = top + 'px';
    }

    function showPopover(anchor) {
        pending = anchor;
        var preview = anchor.quote.length > 60 ? anchor.quote.slice(0, 60) + '…' : anchor.quote;
        popQuote.textContent = '「' + preview + '」';
        popHint.textContent = anchor.tooLong
            ? '选中文字过长（最多 500 字）。'
            : '留空则仅高亮 · Ctrl+Enter 保存 · Esc 取消';
        popover.classList.toggle('is-invalid', !!anchor.tooLong);
        if (popover.hidden) {
            popBody.value = '';
            renderTagChips(popTags, '');
        }
        popover.hidden = false;
        positionPopover();
    }

    function hidePopover() {
        pending = null;
        popover.hidden = true;
        popover.classList.remove('is-invalid');
    }

    function popoverHasFocus() { return popover.contains(document.activeElement); }

    function evaluateSelection() {
        if (popoverHasFocus()) return;
        var anchor = selectionAnchor(contentEl);
        if (anchor) showPopover(anchor);
        else if (!popover.hidden && popBody.value.trim() === '') hidePopover();
    }

    document.addEventListener('selectionchange', function () {
        clearTimeout(selectionTimer);
        selectionTimer = setTimeout(evaluateSelection, 250);
    });
    contentEl.addEventListener('mouseup', function () { setTimeout(evaluateSelection, 0); });
    contentEl.addEventListener('touchend', function () { setTimeout(evaluateSelection, 300); });
    contentEl.addEventListener('keyup', function (e) {
        if (e.shiftKey) setTimeout(evaluateSelection, 0);
    });
    window.addEventListener('resize', positionPopover);
    document.addEventListener('scroll', positionPopover, true);

    function savePopover() {
        if (!pending || pending.tooLong) return;
        var anchor = pending;
        var fields = {
            entry_id: entryId,
            body: popBody.value.trim(),
            quote: anchor.quote,
            tag: selectedTag(popTags),
            anchor_start: anchor.start,
            anchor_end: anchor.end,
            prefix: anchor.prefix,
            suffix: anchor.suffix
        };
        popHint.textContent = '保存中…';
        request('POST', '/api/notes', fields).then(function (data) {
            if (!data) return;
            if (data.error) { popHint.textContent = data.error; return; }
            hidePopover();
            var sel = window.getSelection();
            if (sel) sel.removeAllRanges();
            setHint(fields.body ? '已添加批注。' : '已高亮。');
            refresh().then(function () { flash(document.getElementById('note-' + data.note_id)); });
        }).catch(function () { popHint.textContent = '保存失败，请稍后重试。'; });
    }

    document.getElementById('note-popover-save').addEventListener('click', savePopover);
    document.getElementById('note-popover-cancel').addEventListener('click', function () {
        hidePopover();
        var sel = window.getSelection();
        if (sel) sel.removeAllRanges();
    });
    popBody.addEventListener('keydown', function (e) {
        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); savePopover(); }
        if (e.key === 'Escape') { e.preventDefault(); hidePopover(); }
    });
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && !popover.hidden && !popoverHasFocus()) hidePopover();
    });
    // Keep the selection while interacting with the popover.
    popover.addEventListener('mousedown', function (e) {
        if (e.target.tagName !== 'TEXTAREA') e.preventDefault();
    });

    // --- initial render + deep link ------------------------------------------

    render();
    function followHash() {
        var match = /^#note-(\d+)$/.exec(window.location.hash);
        if (match) setTimeout(function () { focusNote(match[1]); focusHighlight(match[1]); }, 50);
    }
    followHash();
    window.addEventListener('hashchange', followHash);
})();
