'use strict';
const swarmForms = new Map();
let swarmSignature = '';
$('swarm-tab').onclick = () => switchTab('swarm');

function swarmCard(title, text, source) {
  const card = el('article', 'swarm-card');
  card.append(el('strong', '', title), el('p', '', text));
  if (source) card.append(el('p', 'pi-source', source));
  return card;
}

async function refreshSwarm() {
  try {
    const data = await api('/api/swarm');
    const service = data.service || {};
    const members = service.connected_agents || [];
    $('swarm-status').textContent = service.enabled
      ? `${members.length} Pi ${members.length === 1 ? 'agent' : 'agents'} connected · shared MCP server running`
      : 'Swarm paused · enable computer access in Settings to connect agents.';
    $('observation-time').textContent = 'Observed ' + observationTime();
    const signature = JSON.stringify([members.map(m => [m.session_id, m.instance_id]), data.tasks, data.findings, data.messages, data.events, service.enabled]);
    if (signature === swarmSignature) return;
    swarmSignature = signature;
    $('swarm-members').replaceChildren(...members.map(m => swarmCard('Connected Pi', m.session_id, 'Instance ' + m.instance_id)));
    if (!members.length) $('swarm-members').append(el('p', 'pi-empty', 'Pi sessions connect after /reload. New Pi sessions connect automatically.'));
    const recipient = $('swarm-recipient').value;
    const options = members.map(m => { const option = el('option', '', m.session_id); option.value = m.session_id; return option; });
    if (recipient && !members.some(m => m.session_id === recipient)) {
      const option = el('option', '', recipient + ' (disconnected)'); option.value = recipient; options.push(option);
    }
    $('swarm-recipient').replaceChildren(...options);
    if (recipient) $('swarm-recipient').value = recipient;
    const tasks = data.tasks || [], findings = data.findings || [], messages = data.messages || [];
    $('swarm-tasks').replaceChildren(...tasks.map(t => swarmCard(`${t.title || t.task_id || t.id} · ${t.status}`, t.description || t.result || '',
      `Task ${t.id || t.task_id} · owner: ${t.owner || 'unclaimed'}${t.dependencies?.length ? ' · depends on: ' + t.dependencies.join(', ') : ''}`)));
    $('swarm-findings').replaceChildren(...findings.map(f => swarmCard(f.sender || f.actor || 'Finding', f.summary,
      `Finding ${f.id} · ${(f.sources || []).join(' · ')}`)));
    $('swarm-messages').replaceChildren(...messages.map(m => swarmCard(`${m.sender} → ${m.to}`, m.message,
      `Message ${m.id} · ${m.state}${m.parent_id ? ' · reply to ' + m.parent_id : ''}`)));
    for (const [id, records, empty] of [['swarm-tasks', tasks, 'No shared tasks yet.'], ['swarm-findings', findings, 'Agents publish findings with source references here.'], ['swarm-messages', messages, 'Peer and coordinator messages appear here with delivery status.']]) {
      if (!records.length) $(id).append(el('p', 'pi-empty', empty));
    }
    $('swarm-events').replaceChildren(...(data.events || []).map(e => swarmCard(`#${e.id} · ${e.kind || e.type || 'event'}`, e.actor || e.sender || '', e.created_at ? new Date(e.created_at * 1000).toLocaleString() : '')));
    for (const [form, button] of [['swarm-message-form', 'swarm-message-send'], ['swarm-task-form', 'swarm-task-create']]) {
      if (!swarmForms.get(form)?.busy) $(button).disabled = !service.enabled;
    }
  } catch (error) { $('swarm-status').textContent = 'Swarm unavailable: ' + error.message; }
}

function bindSwarmForm(formId, buttonId, statusId, makeRequest) {
  const form = $(formId), button = $(buttonId), normalLabel = button.textContent;
  form.onsubmit = async event => {
    event.preventDefault();
    let pending = swarmForms.get(formId);
    if (pending?.busy) return;
    if (!pending) { pending = {request: makeRequest(), busy: false}; swarmForms.set(formId, pending); }
    pending.busy = true;
    for (const field of form.querySelectorAll('input,textarea,select')) field.disabled = true;
    button.disabled = true; button.textContent = 'Sending…';
    try {
      await api('/api/swarm/action', pending.request);
      swarmForms.delete(formId); form.reset();
      $(statusId).textContent = pending.request.name === 'swarm_send'
        ? 'Queued in the durable mailbox. Delivery does not mean the work is complete.' : 'Task added to the shared board.';
    } catch (error) {
      if (error.status >= 400 && error.status < 500) {
        swarmForms.delete(formId); $(statusId).textContent = error.message;
      } else $(statusId).textContent = 'Result unknown. Retry reuses the same request ID: ' + error.message;
    } finally {
      pending.busy = false;
      const retry = swarmForms.has(formId);
      for (const field of form.querySelectorAll('input,textarea,select')) field.disabled = retry;
      button.disabled = false; button.textContent = retry ? 'Retry same request' : normalLabel;
      await refreshSwarm();
    }
  };
}

bindSwarmForm('swarm-message-form', 'swarm-message-send', 'swarm-message-status', () => ({
  name: 'swarm_send', arguments: {to: $('swarm-recipient').value, message: $('swarm-message').value.trim(), request_id: crypto.randomUUID()}
}));
bindSwarmForm('swarm-task-form', 'swarm-task-create', 'swarm-task-status', () => ({
  name: 'swarm_task', arguments: {action: 'create', task_id: crypto.randomUUID(), title: $('swarm-task-title').value.trim(),
    description: $('swarm-task-description').value.trim(), dependencies: [], request_id: crypto.randomUUID()}
}));
