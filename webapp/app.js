(() => {
  const tg = window.Telegram?.WebApp;
  if (tg) { tg.ready(); tg.expand(); tg.setHeaderColor?.('#111318'); tg.setBackgroundColor?.('#090a0d'); }

  const state = { users: [], user: null, chats: [], chat: null, nextBefore: null, loadingMore: false };
  const $ = (id) => document.getElementById(id);
  const app = $('app');
  const qs = new URLSearchParams(location.search);
  const isLocal = ['localhost', '127.0.0.1'].includes(location.hostname);
  const initData = tg?.initData || '';
  const devUser = isLocal ? qs.get('devUser') || '' : '';
  let toastTimer;

  function toast(message) {
    const el = $('toast'); el.textContent = message; el.classList.add('show');
    clearTimeout(toastTimer); toastTimer = setTimeout(() => el.classList.remove('show'), 2600);
  }
  async function api(path) {
    const headers = { 'X-Telegram-Init-Data': initData };
    if (devUser) headers['X-Holygram-Dev-User'] = devUser;
    const response = await fetch(path, { headers });
    let body = {}; try { body = await response.json(); } catch (_) {}
    if (!response.ok) throw new Error(body.error || `Ошибка ${response.status}`);
    return body;
  }
  function mediaUrl(url, lottie = false) {
    const separator = url.includes('?') ? '&' : '?';
    const params = [];
    if (lottie) params.push('format=lottie');
    if (devUser) params.push(`devUser=${encodeURIComponent(devUser)}`);
    return params.length ? `${url}${separator}${params.join('&')}` : url;
  }
  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>'"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
  }
  function initials(name) {
    const parts = String(name || '?').replace(/\s*\([^)]*\)\s*$/, '').replace(/^@/, '').trim().split(/\s+/).filter(Boolean);
    return (parts.slice(0, 2).map(x => x[0]).join('') || '?').toUpperCase();
  }
  function hue(id) { return Math.abs(Number(id || 0)) % 360; }
  function avatarHtml(person, useId = true) {
    const id = useId ? person.id : person.author_id;
    const name = person.name || person.author || '?';
    const src = id ? `/api/avatar/${id}` : '';
    return `<div class="avatar" style="background:hsl(${hue(id)} 32% 31%)">${src ? `<img src="${src}${devUser ? `?devUser=${encodeURIComponent(devUser)}` : ''}" alt="" loading="lazy">` : ''}<span>${escapeHtml(initials(name))}</span></div>`;
  }
  function setAvatar(el, person) {
    el.style.background = `hsl(${hue(person.id)} 32% 31%)`;
    el.innerHTML = `<img src="/api/avatar/${person.id}${devUser ? `?devUser=${encodeURIComponent(devUser)}` : ''}" alt=""><span>${escapeHtml(initials(person.name))}</span>`;
  }
  function formatTime(ts, withDate = false) {
    if (!ts) return '';
    const date = new Date(ts * 1000);
    const today = new Date();
    if (!withDate && date.toDateString() === today.toDateString()) return date.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', timeZone: 'Europe/Moscow' });
    return date.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', timeZone: 'Europe/Moscow' });
  }
  function skeleton(count = 6) { return Array.from({length: count}, () => '<div class="skeleton"></div>').join(''); }
  function empty(icon, title, text) { return `<div class="empty-icon">${icon}</div><b>${escapeHtml(title)}</b><span>${escapeHtml(text)}</span>`; }
  function debounce(fn, wait = 250) { let timer; return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), wait); }; }

  async function loadUsers(query = '') {
    $('usersList').className = 'list'; $('usersList').innerHTML = skeleton();
    try {
      const data = await api(`/api/users?q=${encodeURIComponent(query)}&limit=80`);
      state.users = data.items;
      $('usersCount').textContent = data.items.length;
      if (!data.items.length) { $('usersList').className = 'list empty-state'; $('usersList').innerHTML = empty('⌕', 'Ничего не найдено', 'Попробуйте другое имя'); return; }
      $('usersList').innerHTML = data.items.map(user => `
        <button class="list-item ${state.user?.id === user.id ? 'active' : ''}" data-user="${user.id}" type="button">
          ${avatarHtml(user)}
          <span class="list-body"><span class="list-line"><span class="list-title">${escapeHtml(user.name)}</span><span class="list-time">${formatTime(user.updated_at)}</span></span>
          <span class="list-preview"><span>${user.username ? '@' + escapeHtml(user.username) + ' · ' : ''}${user.chat_count} чат.</span><span>${user.message_count} сообщ.</span></span></span>
        </button>`).join('');
      document.querySelectorAll('[data-user]').forEach(button => button.addEventListener('click', () => selectUser(Number(button.dataset.user))));
    } catch (error) { showFatal(error.message); }
  }

  async function selectUser(id) {
    state.user = state.users.find(item => item.id === id); state.chat = null;
    if (!state.user) return;
    document.querySelectorAll('[data-user]').forEach(el => el.classList.toggle('active', Number(el.dataset.user) === id));
    $('selectedUserName').textContent = state.user.name;
    $('selectedUserMeta').textContent = `${state.user.username ? '@' + state.user.username + ' · ' : ''}${state.user.chat_count} чат. · ${state.user.message_count} сообщ.`;
    setAvatar($('selectedUserAvatar'), state.user);
    $('chatSearch').disabled = false; $('chatSearch').value = '';
    app.dataset.view = 'chats';
    await loadChats();
  }

  async function loadChats(query = '') {
    if (!state.user) return;
    $('chatsList').className = 'list'; $('chatsList').innerHTML = skeleton();
    try {
      const data = await api(`/api/users/${state.user.id}/chats?q=${encodeURIComponent(query)}`);
      state.chats = data.items;
      if (!data.items.length) { $('chatsList').className = 'list empty-state'; $('chatsList').innerHTML = empty('☰', 'Чатов нет', query ? 'Поиск ничего не нашёл' : 'Сохранённых сообщений пока нет'); return; }
      $('chatsList').innerHTML = data.items.map(chat => `
        <button class="list-item ${state.chat?.id === chat.id ? 'active' : ''}" data-chat="${chat.id}" type="button">
          ${avatarHtml({id: chat.avatar_id, name: chat.name})}
          <span class="list-body"><span class="list-line"><span class="list-title">${chat.pinned ? '📌 ' : ''}${escapeHtml(chat.name)}</span><span class="list-time">${formatTime(chat.updated_at)}</span></span>
          <span class="list-preview"><span>${chat.preview ? escapeHtml(chat.preview) : (chat.media_count ? `Медиа: ${chat.media_count}` : 'Без текста')}</span>${chat.unread ? `<span class="unread">${chat.unread}</span>` : `<span>${chat.message_count}</span>`}</span></span>
        </button>`).join('');
      document.querySelectorAll('[data-chat]').forEach(button => button.addEventListener('click', () => selectChat(Number(button.dataset.chat))));
    } catch (error) { $('chatsList').className = 'list empty-state'; $('chatsList').innerHTML = empty('!', 'Не удалось открыть чаты', error.message); }
  }

  async function selectChat(id) {
    state.chat = state.chats.find(item => item.id === id);
    if (!state.chat) return;
    document.querySelectorAll('[data-chat]').forEach(el => el.classList.toggle('active', Number(el.dataset.chat) === id));
    $('chatName').textContent = state.chat.name;
    $('chatMeta').textContent = `${state.chat.message_count} сообщений · ${state.chat.media_count} медиа`;
    setAvatar($('chatAvatar'), {id: state.chat.avatar_id, name: state.chat.name});
    $('refreshButton').disabled = false;
    app.dataset.view = 'messages';
    await loadMessages(false);
  }

  function mediaMarkup(media) {
    if (!media) return '';
    const spoiler = media.spoiler ? ' spoiler' : '';
    const url = mediaUrl(media.url);
    if (media.type === 'photo') return `<div class="message-media${spoiler}"><img src="${url}" alt="Фото" loading="lazy"></div>`;
    if (['video','animation'].includes(media.type)) return `<div class="message-media${spoiler}"><video src="${url}" controls playsinline preload="metadata"></video></div>`;
    if (media.type === 'video_note') return `<div class="message-media"><video class="round${spoiler}" src="${url}" controls playsinline preload="metadata"></video></div>`;
    if (['voice','audio'].includes(media.type)) return `<div class="message-media"><audio src="${url}" controls preload="metadata"></audio></div>`;
    if (media.type === 'sticker' && media.animated_sticker) return `<div class="message-media sticker-media"><div class="lottie-sticker" data-lottie="${mediaUrl(media.url, true)}"></div></div>`;
    if (media.type === 'sticker' && (media.mime_type.includes('webm') || media.file_name.endsWith('.webm'))) return `<div class="message-media sticker-media"><video src="${url}" autoplay loop muted playsinline></video></div>`;
    if (media.type === 'sticker') return `<div class="message-media sticker-media"><img src="${url}" alt="Стикер" loading="lazy"></div>`;
    const label = media.file_name || ({document:'Файл', audio:'Аудио'}[media.type] || 'Медиа');
    return `<a class="file-card" href="${url}" target="_blank" rel="noopener"><span class="file-icon">↓</span><span>${escapeHtml(label)}</span></a>`;
  }

  function renderMessages(items, prepend = false) {
    const scroll = $('messageScroll');
    const html = items.map(message => `
      <div class="message-row ${message.outgoing ? 'outgoing' : 'incoming'}" data-message="${message.id}">
        <article class="bubble${message.deleted ? ' deleted' : ''}">
          ${!message.outgoing ? `<div class="author">${escapeHtml(message.author)}</div>` : ''}
          ${message.reply ? `<div class="reply"><b>${escapeHtml(message.reply.author)}</b><span>${escapeHtml(message.reply.text || 'Сообщение')}</span></div>` : ''}
          ${mediaMarkup(message.media)}
          ${message.text ? `<div class="message-text">${escapeHtml(message.text)}</div>` : ''}
          ${message.deleted ? '<div class="message-text deleted-label">Сообщение удалено</div>' : ''}
          <div class="message-meta">${message.edited ? `<span>изменено${message.edit_count > 1 ? ' ' + message.edit_count + '×' : ''}</span>` : ''}<time>${formatTime(message.updated_at)}</time>${message.outgoing ? '<span>✓✓</span>' : ''}</div>
        </article>
      </div>`).join('');
    if (prepend) {
      const oldHeight = scroll.scrollHeight; const holder = document.createElement('div'); holder.innerHTML = html;
      const load = scroll.querySelector('.load-more'); Array.from(holder.children).reverse().forEach(node => load?.after(node));
      scroll.scrollTop += scroll.scrollHeight - oldHeight;
    } else { scroll.className = 'message-scroll'; scroll.innerHTML = `${state.nextBefore ? '<button class="load-more" type="button">Показать более ранние</button>' : ''}${html}`; scroll.scrollTop = scroll.scrollHeight; }
    scroll.querySelector('.load-more')?.addEventListener('click', () => loadMessages(true));
    scroll.querySelectorAll('.spoiler').forEach(el => el.addEventListener('click', () => el.classList.add('revealed')));
    initLottie(scroll);
  }

  function initLottie(root) {
    if (!window.lottie) { setTimeout(() => initLottie(root), 250); return; }
    root.querySelectorAll('[data-lottie]:not([data-ready])').forEach(async el => {
      el.dataset.ready = '1';
      try {
        const response = await fetch(el.dataset.lottie, { headers: { 'X-Telegram-Init-Data': initData, ...(devUser ? {'X-Holygram-Dev-User':devUser} : {}) } });
        if (!response.ok) throw new Error();
        const animationData = await response.json();
        window.lottie.loadAnimation({container: el, renderer:'svg', loop:true, autoplay:true, animationData});
      } catch (_) { el.textContent = 'Стикер'; }
    });
  }

  async function loadMessages(older) {
    if (!state.user || !state.chat || (older && (!state.nextBefore || state.loadingMore))) return;
    const scroll = $('messageScroll');
    if (!older) { scroll.className = 'message-scroll'; scroll.innerHTML = skeleton(4); }
    state.loadingMore = true;
    try {
      const before = older ? `&before=${state.nextBefore}` : '';
      const data = await api(`/api/users/${state.user.id}/chats/${state.chat.id}/messages?limit=60${before}`);
      state.nextBefore = data.next_before;
      if (!older && !data.items.length) { scroll.className = 'message-scroll empty-state'; scroll.innerHTML = empty('…', 'Сообщений нет', 'В этом чате пока ничего не сохранено'); return; }
      renderMessages(data.items, older);
      state.chat.unread = 0;
    } catch (error) { if (!older) { scroll.className = 'message-scroll empty-state'; scroll.innerHTML = empty('!', 'Не удалось открыть сообщения', error.message); } else toast(error.message); }
    finally { state.loadingMore = false; }
  }

  function showFatal(message) {
    $('authErrorText').textContent = message;
    $('authError').classList.remove('hidden');
    app.classList.add('hidden');
  }

  $('userSearch').addEventListener('input', debounce(event => loadUsers(event.target.value)));
  $('chatSearch').addEventListener('input', debounce(event => loadChats(event.target.value)));
  $('refreshButton').addEventListener('click', () => { loadMessages(false); loadChats($('chatSearch').value); toast('Обновляем чат'); });
  document.querySelectorAll('[data-back]').forEach(button => button.addEventListener('click', () => { app.dataset.view = button.dataset.back; }));
  document.addEventListener('click', event => { if (event.target.closest('.spoiler')) event.target.closest('.spoiler').classList.add('revealed'); });
  document.addEventListener('error', event => { if (event.target instanceof HTMLImageElement && event.target.closest('.avatar')) event.target.remove(); }, true);

  (async () => {
    try {
      const bootstrap = await api('/api/bootstrap');
      $('ownerAvatar').textContent = initials(bootstrap.owner.name);
      await loadUsers();
    } catch (error) { showFatal(error.message); }
  })();
})();
