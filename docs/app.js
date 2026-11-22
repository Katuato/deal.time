/* Делу.Время — Telegram Mini App. Data stays local until a backend is provided. */
const tg = window.Telegram?.WebApp;
try { tg?.ready(); tg?.expand(); tg?.setHeaderColor?.('#ffffff'); tg?.setBackgroundColor?.('#ffffff'); } catch (_) {}

const STORE = 'dealender-miniapp-v1';
const DEMO_MODE = window.DEALENDER_CONFIG?.demoMode === true;
const seed = {
  onboarded: false, telegramBusiness: false, googleConnected: true,
  notificationsEnabled: true, theme: 'telegram', selectedDate: '2026-10-18',
  month: 9, year: 2026, taskTab: 'active', historyQuery: '', filterOpen: false,
  historyFilter: { date: false, chat: false }, selectedEventId: 'e1',
  tasks: [
    { id:'t1', title:'Встретиться с Дизайнером', time:'10:00', date:'2026-10-18', done:true },
    { id:'t2', title:'Созвониться с Менеджером проекта', time:'12:35', date:'2026-10-18', done:false },
    { id:'t3', title:'Планёрка', time:'13:30', date:'2026-10-18', done:false },
    { id:'t4', title:'Проверить выполнение задач Разработчиками', time:'16:40', date:'2026-10-18', done:false },
    { id:'t5', title:'Связаться с Аналитиком', time:'18:00', date:'2026-10-18', done:false }
  ],
  events: [
    { id:'e1', title:'Встретиться с Дизайнером', date:'2026-10-18', time:'10:00', end:'11:00', repeat:'Не повторяется', source:'Разрешённый чат Telegram Business', sync:'Создано в Google Calendar', chat:'Галина', status:'Создано' },
    { id:'e2', title:'Созвониться с Менеджером проекта', date:'2026-10-18', time:'12:35', end:'13:00', repeat:'Не повторяется', source:'Разрешённый чат Telegram Business', sync:'Создано в Google Calendar', chat:'Аркадий', status:'Создано' },
    { id:'e3', title:'Планёрка', date:'2026-10-18', time:'13:30', end:'14:10', repeat:'Повтор каждую неделю', source:'Разрешённый чат Telegram Business', sync:'Создано в Google Calendar', chat:'Группа', status:'Создано' },
    { id:'e4', title:'Проверить выполнение задач Разработчиками', date:'2026-10-18', time:'16:40', end:'17:00', repeat:'Повтор каждый день', source:'Разрешённый чат Telegram Business', sync:'Создано в Google Calendar', chat:'Группа', status:'Создано' },
    { id:'e5', title:'Связаться с Аналитиком', date:'2026-10-18', time:'18:00', end:'18:30', repeat:'Не повторяется', source:'Разрешённый чат Telegram Business', sync:'Создано в Google Calendar', chat:'Татьяна', status:'Создано' }
  ],
  notices: [
    { id:'n1', title:'Распознано новое событие!', text:'Нажмите, чтобы просмотреть', kind:'recognized', unread:true },
    { id:'n2', title:'Напоминание', text:'Проверить выполнение задач Разработчиками', extra:'Через 30 минут', kind:'task', unread:false },
    { id:'n3', title:'Добавлено новое событие!', text:'Нажмите, чтобы просмотреть карточку события', kind:'event', unread:false },
    { id:'n4', title:'Google Calendar успешно подключен!', text:'', kind:'settings', unread:false },
    { id:'n5', title:'Попробуйте добавить новое событие', text:'Нажмите, чтобы узнать как', kind:'help', unread:false }
  ],
  chats: ['Галина','Аркадий','Анна','Ирина','Дарья'],
  excludedChats: [],
  showAllHome: false
};
function loadState(){
  let saved={};try{saved=JSON.parse(localStorage.getItem(STORE)||'{}')||{}}catch(_){}
  if(DEMO_MODE)return {...structuredClone(seed),...saved,historyFilter:{...seed.historyFilter,...saved.historyFilter}};
  return {onboarded:true,telegramBusiness:false,googleConnected:false,notificationsEnabled:false,theme:saved.theme||'telegram',selectedDate:saved.selectedDate||new Date().toISOString().slice(0,10),month:new Date().getMonth(),year:new Date().getFullYear(),taskTab:saved.taskTab||'active',historyQuery:saved.historyQuery||'',filterOpen:false,historyFilter:{date:false,chat:false,...saved.historyFilter},selectedEventId:null,selectedTaskId:null,events:[],tasks:[],notices:[],chats:[],excludedChats:[],showAllHome:false,historyRange:{from:'',to:''},historyChat:'',apiStatus:'loading',apiError:''};
}
let state=loadState();
let screen=state.onboarded?'home':'onboarding';
let onboardStep=state.telegramBusiness?2:1;
let modal=null;
let toastTimer;
const app=document.getElementById('app');
const icons={
  home:'<svg viewBox="0 0 32 32" fill="none"><path d="M7 12.5 16 5l9 7.5V26a2 2 0 0 1-2 2h-5V18h-4v10H9a2 2 0 0 1-2-2V12.5Z" fill="currentColor"/><path d="M16 5v23h-2V18h4v10h-2" fill="#b8d3d8"/></svg>',
  calendar:'<svg viewBox="0 0 32 32" fill="none"><rect x="4" y="7" width="24" height="22" rx="6" fill="currentColor"/><path d="M4 13h24v10a6 6 0 0 1-6 6H10a6 6 0 0 1-6-6V13Z" fill="#b6d4d9"/><path d="M10 4v6M22 4v6" stroke="currentColor" stroke-width="3" stroke-linecap="round"/><circle cx="11" cy="18" r="1.5" fill="white"/><circle cx="17" cy="18" r="1.5" fill="white"/><circle cx="23" cy="18" r="1.5" fill="white"/></svg>',
  tasks:'<svg viewBox="0 0 32 32" fill="none"><rect x="5" y="4" width="22" height="24" rx="6" fill="#b6d4d9"/><path d="m11 16 3 3 7-7" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  history:'<svg viewBox="0 0 32 32" fill="none"><rect x="6" y="4" width="20" height="24" rx="6" fill="#b6d4d9"/><circle cx="16" cy="11" r="1.6" fill="currentColor"/><path d="M16 16v7" stroke="currentColor" stroke-width="2.8" stroke-linecap="round"/></svg>',
  mic:'<svg viewBox="0 0 48 48" fill="none"><rect x="18" y="3" width="12" height="27" rx="6" stroke="currentColor" stroke-width="4"/><path d="M10 23a14 14 0 0 0 28 0M24 37v8M16 45h16" stroke="currentColor" stroke-width="4" stroke-linecap="round"/></svg>',
  voice:'<svg viewBox="0 0 48 48" fill="none"><circle cx="17" cy="12" r="5" stroke="currentColor" stroke-width="4"/><path d="M5 34c0-8 5-12 12-12s12 4 12 12v3H5v-3ZM31 9c6 4 6 12 0 16M36 4c10 8 10 18 0 26" stroke="currentColor" stroke-width="4" stroke-linecap="round"/></svg>',
  search:'<svg viewBox="0 0 24 24" fill="none"><circle cx="10" cy="10" r="7" stroke="currentColor" stroke-width="2"/><path d="m15 15 6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
  filter:'<svg viewBox="0 0 28 28" fill="none"><path d="M3 5h22l-9 10v8l-4 2V15L3 5Z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/></svg>'
};
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const id=()=>Math.random().toString(36).slice(2,10);
function persist(){
  if(DEMO_MODE){localStorage.setItem(STORE,JSON.stringify(state));return}
  const preferences={theme:state.theme,selectedDate:state.selectedDate,month:state.month,year:state.year,taskTab:state.taskTab,historyQuery:state.historyQuery,historyFilter:state.historyFilter,historyRange:state.historyRange,historyChat:state.historyChat};
  localStorage.setItem(STORE,JSON.stringify(preferences));
}
function eventFromApi(event){return {...event,id:String(event.id),title:String(event.title||''),date:String(event.date),time:String(event.time||'00:00'),end:String(event.end||event.time||'00:00'),repeat:String(event.repeat||'Не повторяется'),source:String(event.source||'Telegram'),sync:String(event.sync||'Не синхронизировано'),chat:String(event.chat||''),status:String(event.status||'Создано')};}
function taskFromApi(task){return {...task,id:String(task.id),title:String(task.title||''),date:String(task.date),time:String(task.time||'00:00'),done:Boolean(task.done)};}
function notificationFromApi(item){return {...item,id:String(item.id),title:String(item.title||''),text:String(item.text||''),extra:String(item.extra||''),kind:String(item.kind||'event'),unread:Boolean(item.unread)};}
function monthBounds(){const from=`${state.year}-${String(state.month+1).padStart(2,'0')}-01`;const lastDay=new Date(state.year,state.month+1,0).getDate();const to=`${state.year}-${String(state.month+1).padStart(2,'0')}-${String(lastDay).padStart(2,'0')}`;return {from,to};}
async function refreshEvents(){const {from,to}=monthBounds();const result=await DealenderApi.events(from,to);state.events=(result?.events||[]).map(eventFromApi);if(state.selectedEventId&&!state.events.some(e=>e.id===state.selectedEventId))state.selectedEventId=null;}
async function loadIntegrationData(){
  state.apiStatus='loading';state.apiError='';render();
  try{
    const [me,events,tasks,notifications,google]=await Promise.all([DealenderApi.me(),(async()=>{const {from,to}=monthBounds();return DealenderApi.events(from,to)})(),DealenderApi.tasks(),DealenderApi.notifications(),DealenderApi.googleStatus()]);
    state.events=(events?.events||[]).map(eventFromApi);state.tasks=(tasks?.tasks||[]).map(taskFromApi);state.notices=(notifications?.notifications||[]).map(notificationFromApi);state.googleConnected=Boolean(google?.connected);state.notificationsEnabled=Boolean(notifications?.enabled);state.timezone=me?.user?.timezone||'Europe/Moscow';state.apiStatus='ready';render();
  }catch(error){state.apiStatus='error';state.apiError=error.message||'Не удалось загрузить данные.';render();}
}
function renderApiStatus(){const loading=state.apiStatus==='loading';return `${header(false)}<main class="screen"><div class="card empty" role="status"><h2>${loading?'Подключаем Mini App…':'Не удалось подключиться к серверу'}</h2><p>${loading?'Загружаем ваши события и настройки.':esc(state.apiError)}</p>${loading?'':`<p>Проверьте API и откройте приложение внутри Telegram. Для просмотра макета добавьте <code>?demo=1</code> к адресу.</p><button class="btn wide" data-action="retry-api">Повторить</button>`}</div></main>${footer()}`;}
function feedback(message){const el=document.getElementById('toast');el.textContent=message;el.classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.classList.remove('show'),3100);try{tg?.HapticFeedback?.impactOccurred?.('light')}catch(_){}}
function navigate(next){screen=next;modal=null;state.filterOpen=false;render();window.scrollTo(0,0)}
function header(quick=true){return `<div class="tg-chrome"><button class="tg-close" data-action="close" aria-label="Закрыть"><span>×</span>Закрыть</button><button class="tg-menu" data-action="menu" aria-label="Меню">⌄ <span class="dots">•••</span></button></div><header class="brand-header"><img class="logo" src="./assets/logo.png" alt="Делу.Время"><svg class="brand-wave" viewBox="0 0 315 28" preserveAspectRatio="none" aria-hidden="true"><path d="M0 22C60 28 96 1 133 9c21 4 23 25 47 14S240-1 262 6c14 5 15 30 53 4"/></svg></header>${quick?`<div class="quick-actions"><button class="circle-action" data-nav="settings" title="Настройки" aria-label="Настройки">⚙️</button><button class="circle-action" data-nav="notifications" title="Уведомления" aria-label="Уведомления">🔔</button></div>`:''}`}
function tabs(active){return `<nav class="tabbar" aria-label="Основные разделы">${[['home','Главная'],['calendar','Календарь'],['tasks','Задачи'],['history','История']].map(([key,label])=>`<button class="tab ${active===key?'active':''}" data-nav="${key}" aria-label="${label}" aria-current="${active===key?'page':'false'}">${icons[key]}</button>`).join('')}</nav>`}
function footer(){return `<a class="footer-help" href="#" data-nav="help">Как это работает?</a>`}
function subhead(icon,title,back='settings'){return `<div class="back-title"><button class="back-link" data-nav="${back}" aria-label="Назад">‹</button><span class="heading-icon">${icon}</span><span class="title-pill">${title}</span></div>`}
function dateLabel(date,full=false){if(date==='2026-10-18')return full?'18 октября 2026, понедельник':'понедельник, 18 октября';const d=new Date(`${date}T12:00:00`);return new Intl.DateTimeFormat('ru-RU',{weekday:'long',day:'numeric',month:'long',...(full?{year:'numeric'}:{})}).format(d)}
function shortDate(date){const d=new Date(`${date}T12:00:00`);return new Intl.DateTimeFormat('ru-RU',{day:'numeric',month:'long'}).format(d)}
function eventsFor(date){return state.events.filter(e=>e.date===date).sort((a,b)=>a.time.localeCompare(b.time))}
function renderOnboarding(){return `<div class="onboarding"><div class="tg-chrome"><button class="tg-close" data-action="close" aria-label="Закрыть"><span>×</span>Закрыть</button><button class="tg-menu" data-action="menu" aria-label="Меню">⌄ <span class="dots">•••</span></button></div><main class="screen"><img class="onboard-logo" src="./assets/logo.png" alt="Делу.Время"><p class="onboard-tag">сервис для поиска договоренностей в сообщениях и создания событий в Google Calendar</p><div class="greeting"><small>Понедельник, 18 окт.</small><h1>Привет!<br>Какие планы на сегодня?</h1><svg class="wave-rule" viewBox="0 0 340 28" preserveAspectRatio="none"><path d="M0 23C61 29 101 2 137 10c17 4 19 20 38 14S230 0 253 7s17 28 46 19 33-16 41-19"/></svg></div><div class="onboard-cta"><p>${onboardStep===1?'Чтобы начать планирование необходимо подключить Telegram Business':'Давай посмотрим, о каких событиях и встречах ты уже успел договориться'}</p><svg class="onboard-arrow" viewBox="0 0 80 75"><path d="M28 5C-7 18 6 58 26 55c27-3 5-48-14-19S22 78 74 63m-12-9 12 9-9 11"/></svg><button class="btn teal" data-action="onboard">${onboardStep===1?'Подключить':'Мои планы'}</button></div></main>${footer()}</div>`}
function renderHome(){const list=eventsFor(state.selectedDate);const shown=state.showAllHome?list:list.slice(0,3);return `${header()}<main class="screen with-tabs"><div class="card home-date">Сегодня, ${shortDate(state.selectedDate)}</div><div class="home-agenda">${shown.map(e=>`<button class="card home-event clickable" data-event="${e.id}"><span class="time">${esc(e.time)} - ${esc(e.end)}</span><span class="card-title">${esc(e.title)}</span></button>`).join('')||'<div class="card empty">На этот день пока нет событий</div>'}</div>${list.length>3?`<button class="btn show-more" data-action="show-more">${state.showAllHome?'Скрыть':'Показать больше'}</button>`:''}<div class="voice-panel"><button class="voice-button" data-action="voice">${icons.voice}<span>Голос</span></button><button class="voice-button" data-action="parse">${icons.mic}<span>Разобрать</span></button></div></main>${tabs('home')}${footer()}`}
function renderCalendar(){const first=new Date(state.year,state.month,1);const days=new Date(state.year,state.month+1,0).getDate();const offset=state.year===2026&&state.month===9?4:(first.getDay()+6)%7;const selected=state.selectedDate;const months=[...Array(12)].map((_,i)=>`<option value="${i}" ${i===state.month?'selected':''}>${new Intl.DateTimeFormat('ru-RU',{month:'long'}).format(new Date(2026,i,1)).toUpperCase()}</option>`).join('');const years=[2024,2025,2026,2027,2028,2029,2030].map(y=>`<option value="${y}" ${y===state.year?'selected':''}>${y}</option>`).join('');const dayCells=[...Array(offset)].map(()=>'<span></span>').concat([...Array(days)].map((_,i)=>{const date=`${state.year}-${String(state.month+1).padStart(2,'0')}-${String(i+1).padStart(2,'0')}`;return `<button class="calendar-day ${date===selected?'selected':''} ${eventsFor(date).length?'has-event':''}" data-date="${date}" aria-label="${dateLabel(date)}">${i+1}</button>`}));return `${header()}<main class="screen with-tabs"><div class="calendar-box"><div class="calendar-controls"><span class="select-wrap"><select id="month-select" aria-label="Месяц">${months}</select></span><span class="select-wrap"><select id="year-select" aria-label="Год">${years}</select></span></div><div class="calendar-grid">${['ПН','ВТ','СР','ЧТ','ПТ','СБ','ВС'].map(d=>`<span class="calendar-weekday">${d}</span>`).join('')}${dayCells.join('')}</div></div><div class="card calendar-agenda"><h2>${selected==='2026-10-18'?'Сегодня, 18 октября':dateLabel(selected)}</h2>${eventsFor(selected).map(e=>`<div class="agenda-line"><time>${esc(e.time)}</time><button data-event="${e.id}">${esc(e.title)}</button></div>`).join('')||'<p class="empty">На выбранную дату событий нет</p>'}</div></main>${tabs('calendar')}${footer()}`}
function renderTasks(){const done=state.taskTab==='done';const filtered=state.tasks.filter(t=>Boolean(t.done)===done);return `${header()}<main class="screen with-tabs"><h1 class="page-title">Мои задачи</h1><div class="segmented"><button data-action="task-tab" data-value="active" class="${!done?'selected':''}">Активные</button><button data-action="task-tab" data-value="done" class="${done?'selected':''}">Выполненные</button></div><div class="task-list">${filtered.map(t=>`<button class="card task-card clickable ${done?'done':''}" data-task="${t.id}">${done?'<span class="done-mark">✅</span>':''}<span class="card-title">${esc(t.title)} ${done?'<span class="task-menu">•••</span>':''}</span><span class="subtitle">Сегодня · ${esc(t.time)}</span></button>`).join('')||`<div class="card empty">${done?'Выполненных задач пока нет':'Активных задач пока нет'}</div>`}</div>${!done?'<button class="btn task-add" data-nav="task-new">+ Добавить</button>':''}</main>${tabs('tasks')}${footer()}`}
function renderSettings(){return `${header(false)}<main class="screen">${subhead('⚙️','Настройки','home')}<div class="settings-list"><div class="settings-item"><strong>Уведомления</strong><button class="toggle ${state.notificationsEnabled?'on':''}" data-action="toggle-notifications" aria-pressed="${state.notificationsEnabled}">${state.notificationsEnabled?'Вкл':'Выкл'}</button></div>${[['Google Calendar',state.googleConnected?'Подключён':'Не подключён','google'],['Доступ к чатам','Посмотреть разрешения','chats'],['Согласие и данные','Политика обработки данных','privacy'],['Тема',state.theme==='telegram'?'Как в Telegram':state.theme==='light'?'Светлая':'Тёмная','theme'],['FAQ','Часто задаваемые вопросы','faq'],['Поддержка','','support']].map(([title,subtitle,target])=>`<button class="settings-item clickable" data-nav="${target}"><span><strong>${title}</strong>${subtitle?`<span class="muted">${subtitle}</span>`:''}</span><span class="chevron">›</span></button>`).join('')}</div></main>${footer()}`}
const chatColors=['#0081a7','#00afb9','#fdfcdc','#fed9b7','#f07167','#0081a7','#00afb9','#fdfcdc'];
function chatRows(checkbox){return ['Галина','Аркадий','Анна','Ирина','Дарья','Татьяна','Михаил','Валерия'].map((name,i)=>`<label class="chat-row"><span class="avatar" style="background:${chatColors[i]}"></span><span>${name}</span>${checkbox?`<input type="checkbox" data-chat="${name}" ${state.chats.includes(name)?'checked':''}>`:''}</label>`).join('')}
function renderChats(){return `${header(false)}<main class="screen">${subhead('💬','Доступ к чатам','settings')}<div class="chat-group-label">ТИПЫ ЧАТОВ</div><div class="chat-panel">${['Контакты','Не контакты','Группы'].map((x,i)=>`<label class="chat-row"><span class="avatar" style="background:${['#00afb9','#fed9b7','#0081a7'][i]}">${['♙','♧','♟'][i]}</span>${x}<input type="checkbox" data-chat-type="${x}" ${i<2?'checked':''}></label>`).join('')}</div><div class="chat-group-label">ЧАТЫ</div><div class="chat-panel">${chatRows(true)}</div><p class="chat-note">Выберите чаты или типы чатов, которым можно предоставить доступ для распознавания событий.</p><div class="chat-group-label">ИСКЛЮЧЁННЫЕ ЧАТЫ</div><button class="add-chat" data-action="exclude-chat">＋ Добавить исключённые чаты</button></main>${footer()}`}
function renderTheme(){return `${header(false)}<main class="screen">${subhead('🎨','Тема','settings')}<div class="card theme-panel"><h2>Выберите тему для приложения</h2>${[['telegram','Как в Telegram'],['light','Светлая'],['dark','Тёмная']].map(([value,label])=>`<button class="theme-option ${state.theme===value?'selected':''}" data-theme="${value}">${label}<span class="radio"></span></button>`).join('')}</div></main>${footer()}`}
function renderNotifications(){return `${header(false)}<main class="screen">${subhead('🔔','Центр уведомлений','home')}<div class="notice-list">${state.notices.map(n=>`<button class="card notice clickable ${n.unread?'unread':''}" data-notice="${n.id}"><strong>${esc(n.title)}</strong>${n.text?`<span>${esc(n.text)}</span>`:''}${n.extra?`<small>${esc(n.extra)}</small>`:''}</button>`).join('')}</div></main>${footer()}`}
function renderHistory(){let list=[...state.events].sort((a,b)=>b.date.localeCompare(a.date)||a.time.localeCompare(b.time));const q=state.historyQuery.trim().toLocaleLowerCase('ru');if(q)list=list.filter(e=>e.title.toLocaleLowerCase('ru').includes(q)||e.chat.toLocaleLowerCase('ru').includes(q));if(state.historyFilter.chat)list=list.filter(e=>e.chat!=='Группа');if(state.historyFilter.date)list=list.filter(e=>e.date===state.selectedDate);return `${header()}<main class="screen with-tabs"><h1 class="page-title">История</h1><label class="searchbar">${icons.search}<input id="history-search" placeholder="Введите название события" value="${esc(state.historyQuery)}" aria-label="Поиск событий"></label><div class="filter-row">${state.filterOpen?`<div class="filter-popover"><strong>Фильтр</strong><label><input type="checkbox" data-filter="date" ${state.historyFilter.date?'checked':''}> По дате</label><label><input type="checkbox" data-filter="chat" ${state.historyFilter.chat?'checked':''}> По чату</label></div>`:''}<button class="filter-button" data-action="filter" aria-label="Фильтр">${icons.filter}</button></div><p class="history-date">${dateLabel(state.selectedDate).replace(',', ' ·')}</p><div class="history-list">${list.map(e=>`<button class="card history-card clickable" data-event="${e.id}"><span class="card-title">${esc(e.title)}</span><span class="muted">${esc(e.time)} - ${esc(e.end)}<br>${esc(e.repeat==='Не повторяется'?e.status:e.repeat)}</span><span class="chevron">›</span></button>`).join('')||'<div class="card empty">События не найдены</div>'}</div></main>${tabs('history')}${footer()}`}
function field(label,value){return `<div class="field"><span class="field-label">${label}</span><span class="field-value">${esc(value)}</span></div>`}
function renderEvent(){const e=state.events.find(x=>x.id===state.selectedEventId)||state.events[0];return `${header(false)}<main class="screen">${subhead('','Карточка события',state.returnTo||'history')}<div class="event-details"><h2>${esc(e.title)}</h2>${field('Дата',dateLabel(e.date,true))}${field('Время',`${e.time} – ${e.end}`)}${field('Повторение',e.repeat)}${field('Источник',e.source)}${field('Синхронизация',e.sync)}</div><div class="event-actions"><button class="btn wide" data-action="open-google">Открыть в Google Calendar</button><button class="btn soft wide" data-action="delete-event">Удалить</button></div></main>${footer()}`}
function renderRecognized(){return `${header(false)}<main class="screen"><div class="back-title"><button class="back-link" data-nav="notifications" aria-label="Назад">‹</button><h1 class="section-heading">Новое распознанное событие</h1></div><p class="recognized-label">Твоё сообщение</p><div class="card message-card">Завтра надо встретиться с Дизайнером обсудить детали его работы часов в 10</div><div class="found-ribbon">🌟 Я нашёл событие</div><div class="event-details"><h2>Встретиться с Дизайнером</h2>${field('Дата','18 октября 2026, понедельник')}${field('Время','10:00')}${field('Напоминание','Добавить напоминание')}</div><div class="event-actions"><button class="btn wide" data-action="save-recognized">Сохранить событие</button><div class="btn-row"><button class="btn soft" data-action="dismiss-recognized">Удалить</button><button class="btn soft" data-action="recognized-details">Подробнее</button></div></div></main>${footer()}`}
function renderTaskNew(){return `${header(false)}<main class="screen">${subhead('☑','Новая задача','tasks')}<form class="form" id="task-form"><label>Название задачи<input class="form-input" name="title" required maxlength="120" placeholder="Что нужно сделать?"></label><div class="btn-row"><label>Дата<input class="form-input" type="date" name="date" required value="${state.selectedDate}"></label><label>Время<input class="form-input" type="time" name="time" required value="10:00"></label></div><button class="btn wide" type="submit">Добавить задачу</button></form></main>${footer()}`}
function renderTaskDetail(){const t=state.tasks.find(x=>x.id===state.selectedTaskId);if(!t)return renderTasks();return `${header(false)}<main class="screen">${subhead('☑','Моя задача','tasks')}<div class="event-details"><h2>${esc(t.title)}</h2>${field('Дата',dateLabel(t.date,true))}${field('Время',t.time)}${field('Статус',t.done?'Выполнена':'Активная')}</div><div class="event-actions"><button class="btn wide" data-action="toggle-task">${t.done?'Вернуть в активные':'Отметить выполненной'}</button><button class="btn soft wide" data-action="delete-task">Удалить задачу</button></div></main>${footer()}`}
function renderGoogle(){return `${header(false)}<main class="screen">${subhead('📅','Google Calendar','settings')}<div class="card"><h2 class="section-heading">${state.googleConnected?'Календарь подключён':'Подключить календарь'}</h2><p class="muted" style="font-size:13px;line-height:1.5">${state.googleConnected?'События могут синхронизироваться с Google Calendar.':'Подключите Google Calendar, чтобы сохранять найденные события.'}</p><button class="btn ${state.googleConnected?'soft':'teal'} wide" data-action="google-connect">${state.googleConnected?'Отключить':'Подключить'}</button></div></main>${footer()}`}
function renderInfo(){const copy={privacy:['Согласие и данные','Приложение показывает события и задачи из разрешённых чатов. Управлять доступом к чатам можно в настройках.'],faq:['Часто задаваемые вопросы','Как добавить событие? Нажмите «Разобрать» на главной и опишите встречу. Как открыть календарь? Перейдите на вкладку с иконкой календаря.'],support:['Поддержка','Если возник вопрос, напишите владельцу бота в Telegram.'],help:['Как это работает?','Делу.Время помогает находить договорённости в сообщениях, создавать события и напоминания. Разрешите нужные чаты, а затем проверяйте найденные встречи на главной.']}[screen];return `${header(false)}<main class="screen">${subhead('ℹ️',copy[0],screen==='help'?'home':'settings')}<div class="card"><p style="font-size:13px;line-height:1.6">${copy[1]}</p></div></main>${footer()}`}
function renderModal(){if(!modal)return '';if(modal==='parse')return `<div class="modal-overlay" data-action="modal-close"><div class="modal-card" role="dialog" aria-modal="true" aria-label="Разобрать сообщение"><h2>Разобрать сообщение</h2><p>Напишите договорённость или задачу. Мы покажем её в истории.</p><form id="parse-form" class="form"><textarea class="form-input" name="message" required placeholder="Завтра в 10:00 встреча с дизайнером"></textarea><div class="btn-row"><button type="button" class="btn soft" data-action="modal-close">Отмена</button><button type="submit" class="btn blue">Разобрать</button></div></form></div></div>`;if(modal==='google')return `<div class="modal-overlay" data-action="modal-close"><div class="modal-card" role="dialog" aria-modal="true"><h2>Открыть ссылку?</h2><div class="link-card">https://calendar.google.com</div><div class="btn-row"><button class="btn soft" data-action="modal-close">Отмена</button><button class="btn blue" data-action="confirm-google">Открыть</button></div></div></div>`;return `<div class="modal-overlay" data-action="modal-close"><div class="modal-card" role="dialog" aria-modal="true"><h2>${esc(modal.title)}</h2><p>${esc(modal.text)}</p><div class="btn-row"><button class="btn soft" data-action="modal-close">Отмена</button><button class="btn blue" data-action="${modal.action}">${esc(modal.button)}</button></div></div></div>`}
function render(){app.classList.toggle('dark',state.theme==='dark'||state.theme==='telegram'&&tg?.colorScheme==='dark');const pages={onboarding:renderOnboarding,home:renderHome,calendar:renderCalendar,tasks:renderTasks,settings:renderSettings,chats:renderChats,theme:renderTheme,notifications:renderNotifications,history:renderHistory,event:renderEvent,recognized:renderRecognized,'task-new':renderTaskNew,'task-detail':renderTaskDetail,google:renderGoogle,privacy:renderInfo,faq:renderInfo,support:renderInfo,help:renderInfo};app.innerHTML=(pages[screen]||renderHome)()+renderModal();}

app.addEventListener('click',async ev=>{
  const nav=ev.target.closest('[data-nav]');if(nav){ev.preventDefault();navigate(nav.dataset.nav);return}
  const day=ev.target.closest('[data-date]');if(day){state.selectedDate=day.dataset.date;persist();render();return}
  const event=ev.target.closest('[data-event]');if(event){state.selectedEventId=event.dataset.event;state.returnTo=screen;persist();navigate('event');return}
  const task=ev.target.closest('[data-task]');if(task){state.selectedTaskId=task.dataset.task;persist();navigate('task-detail');return}
  const notice=ev.target.closest('[data-notice]');if(notice){const n=state.notices.find(x=>x.id===notice.dataset.notice);if(!n)return;if(!DEMO_MODE){try{await DealenderApi.markNotificationRead(n.id);n.unread=false}catch(error){feedback(error.message);return}}else{n.unread=false;persist()}navigate(({recognized:'recognized',event:'event',settings:'settings',help:'help',task:'tasks'})[n.kind]||'notifications');return}
  const theme=ev.target.closest('[data-theme]');if(theme){state.theme=theme.dataset.theme;persist();render();feedback('Тема изменена');return}
  const action=ev.target.closest('[data-action]');if(!action)return;
  const name=action.dataset.action;
  if(name==='modal-close'){if(ev.target===action||action.tagName==='BUTTON'){modal=null;render()}return}
  if(name==='close'){try{tg?.close()}catch(_){navigate('home')}if(!tg)navigate('home');return}
  if(name==='menu'){modal={title:'Меню',text:'Перейти к настройкам приложения?',action:'go-settings',button:'Настройки'};render();return}
  if(name==='go-settings'){navigate('settings');return}
  if(name==='retry-api'){loadIntegrationData();return}
  if(name==='onboard'){if(!DEMO_MODE){navigate('settings');feedback('Подключите Telegram Business в настройках Telegram');return}if(onboardStep===1){state.telegramBusiness=true;onboardStep=2;persist();render();feedback('Перейдите к своим планам')}else{state.onboarded=true;persist();navigate('home')}return}
  if(name==='show-more'){state.showAllHome=!state.showAllHome;persist();render();return}
  if(name==='task-tab'){state.taskTab=action.dataset.value;persist();render();return}
  if(name==='toggle-notifications'){const enabled=!state.notificationsEnabled;if(!DEMO_MODE){try{await DealenderApi.notificationSettings(enabled);state.notificationsEnabled=enabled;render();feedback(enabled?'Уведомления включены':'Уведомления выключены')}catch(error){feedback(error.message)}return}state.notificationsEnabled=enabled;persist();render();feedback(state.notificationsEnabled?'Уведомления включены':'Уведомления выключены');return}
  if(name==='filter'){state.filterOpen=!state.filterOpen;render();return}
  if(name==='parse'){modal='parse';render();return}
  if(name==='voice'){startVoice();return}
  if(name==='exclude-chat'){modal={title:'Исключённые чаты',text:'Выбранные чаты будут исключены из распознавания событий.',action:'confirm-exclude',button:'Понятно'};render();return}
  if(name==='confirm-exclude'){modal=null;render();return}
  if(name==='google-connect'){if(DEMO_MODE){if(state.googleConnected){modal={title:'Отключить календарь?',text:'Демо-режим: это действие не меняет подключение Google.',action:'confirm-disconnect',button:'Отключить'}}else{feedback('Демо-режим: подключение Google не выполняется')}render();return}if(state.googleConnected){modal={title:'Отключить календарь?',text:'Синхронизация с Google Calendar прекратится.',action:'confirm-disconnect',button:'Отключить'};render();return}try{const result=await DealenderApi.googleConnect();if(!result?.authorization_url||!/^https:\/\//i.test(result.authorization_url))throw new Error('Сервер не вернул безопасную ссылку Google OAuth.');if(tg?.openLink)tg.openLink(result.authorization_url);else window.location.assign(result.authorization_url)}catch(error){feedback(error.message)}return}
  if(name==='confirm-disconnect'){if(!DEMO_MODE){try{await DealenderApi.googleDisconnect();state.googleConnected=false;modal=null;render();feedback('Календарь отключён')}catch(error){feedback(error.message)}return}state.googleConnected=false;persist();modal=null;render();feedback('Демо-состояние изменено');return}
  if(name==='open-google'){modal='google';render();return}
  if(name==='confirm-google'){const url='https://calendar.google.com';if(tg?.openLink)tg.openLink(url);else window.open(url,'_blank','noopener,noreferrer');modal=null;render();return}
  if(name==='delete-event'){modal={title:'Удалить событие?',text:'Событие исчезнет из календаря и истории приложения.',action:'confirm-delete-event',button:'Удалить'};render();return}
  if(name==='confirm-delete-event'){if(!DEMO_MODE){try{await DealenderApi.deleteEvent(state.selectedEventId);state.events=state.events.filter(x=>x.id!==state.selectedEventId);modal=null;navigate('history');feedback('Событие удалено')}catch(error){feedback(error.message)}return}state.events=state.events.filter(x=>x.id!==state.selectedEventId);persist();navigate('history');feedback('Событие удалено');return}
  if(name==='toggle-task'){const t=state.tasks.find(x=>x.id===state.selectedTaskId);if(!t)return;if(!DEMO_MODE){try{const result=await DealenderApi.updateTask(t.id,{done:!t.done});Object.assign(t,taskFromApi(result?.task||{...t,done:!t.done}));navigate('tasks');feedback(t.done?'Задача выполнена':'Задача снова активна')}catch(error){feedback(error.message)}return}t.done=!t.done;persist();navigate('tasks');feedback(t.done?'Задача выполнена':'Задача снова активна');return}
  if(name==='delete-task'){modal={title:'Удалить задачу?',text:'Задача будет удалена из списка.',action:'confirm-delete-task',button:'Удалить'};render();return}
  if(name==='confirm-delete-task'){if(!DEMO_MODE){try{await DealenderApi.deleteTask(state.selectedTaskId);state.tasks=state.tasks.filter(x=>x.id!==state.selectedTaskId);modal=null;navigate('tasks');feedback('Задача удалена')}catch(error){feedback(error.message)}return}state.tasks=state.tasks.filter(x=>x.id!==state.selectedTaskId);persist();navigate('tasks');feedback('Задача удалена');return}
  if(name==='save-recognized'){navigate('event');feedback('Событие сохранено');return}
  if(name==='dismiss-recognized'){navigate('notifications');feedback('Предложение удалено');return}
  if(name==='recognized-details'){state.selectedEventId='e1';state.returnTo='recognized';navigate('event');return}
});
app.addEventListener('change',ev=>{const t=ev.target;if(t.id==='month-select'||t.id==='year-select'){state.month=Number(document.getElementById('month-select').value);state.year=Number(document.getElementById('year-select').value);state.selectedDate=`${state.year}-${String(state.month+1).padStart(2,'0')}-01`;persist();render();return}if(t.dataset.filter){state.historyFilter[t.dataset.filter]=t.checked;persist();render();return}if(t.dataset.chat){state.chats=t.checked?[...new Set([...state.chats,t.dataset.chat])]:state.chats.filter(x=>x!==t.dataset.chat);persist();feedback('Разрешения сохранены');return}if(t.dataset.chatType){feedback('Тип чатов обновлён');return}});
app.addEventListener('change',async ev=>{if(DEMO_MODE||!['month-select','year-select'].includes(ev.target.id)||state.apiStatus!=='ready')return;try{await refreshEvents();render()}catch(error){feedback(error.message)}});
app.addEventListener('input',ev=>{if(ev.target.id==='history-search'){const pos=ev.target.selectionStart;state.historyQuery=ev.target.value;persist();render();const next=document.getElementById('history-search');next.focus();next.setSelectionRange(pos,pos)}});
app.addEventListener('submit',async ev=>{
  ev.preventDefault();const form=ev.target;
  if(form.id==='task-form'){
    const data=new FormData(form);const title=String(data.get('title')).trim();if(!title)return;
    const task={title,time:String(data.get('time')),date:String(data.get('date')),done:false};
    if(!DEMO_MODE){try{const result=await DealenderApi.createTask(task);state.tasks.push(taskFromApi(result.task));navigate('tasks');feedback('Задача добавлена')}catch(error){feedback(error.message)}return}
    state.tasks.push({id:id(),...task});persist();navigate('tasks');feedback('Задача добавлена');return;
  }
  if(form.id==='parse-form'){
    const message=String(new FormData(form).get('message')).trim();if(!message)return;
    if(!DEMO_MODE){try{const result=await DealenderApi.parseEvent(message);if(!result?.event)throw new Error('Сервер не вернул распознанное событие.');const item=eventFromApi(result.event);state.events=[...state.events.filter(e=>e.id!==item.id),item];state.selectedEventId=item.id;state.returnTo='history';modal=null;navigate('event');feedback('Событие обработано сервером')}catch(error){feedback(error.message)}return}
    const match=message.match(/(?:в\s*)?(\d{1,2})[:.](\d{2})/);const time=match?`${match[1].padStart(2,'0')}:${match[2]}`:'10:00';const title=message.length>65?message.slice(0,62)+'…':message;const item={id:id(),title,date:state.selectedDate,time,end:'11:00',repeat:'Не повторяется',source:'Введено вручную',sync:'Демо-режим',chat:'Вручную',status:'Создано'};state.events.push(item);state.selectedEventId=item.id;state.returnTo='history';persist();navigate('event');feedback('Демо-событие добавлено');
  }
});
function startVoice(){const Recognition=window.SpeechRecognition||window.webkitSpeechRecognition;if(!Recognition){feedback('Запись голоса недоступна в этом браузере. Используйте «Разобрать».');return}const recognition=new Recognition();recognition.lang='ru-RU';recognition.interimResults=false;recognition.onresult=ev=>{modal='parse';render();document.querySelector('#parse-form textarea').value=ev.results[0][0].transcript;feedback('Голос распознан')};recognition.onerror=()=>feedback('Не удалось распознать речь');recognition.start();feedback('Говорите — я слушаю')}

/* Functional requirements for the Mini App. The current repository has no HTTP API. */
state.historyRange ||= { from: '', to: '' };
state.historyChat ||= '';

const previousHeader = header;
header = function (quick = true) {
  return previousHeader(quick)
    .replace('>⚙️</button>', '><svg class="quick-icon" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M10 2h4l.6 2.4 1.9.8 2.1-1.2 2.8 2.8-1.2 2.1.8 1.9 2.4.6v4l-2.4.6-.8 1.9 1.2 2.1-2.8 2.8-2.1-1.2-1.9.8L14 24h-4l-.6-2.4-1.9-.8-2.1 1.2-2.8-2.8 1.2-2.1-.8-1.9L.6 14v-4L3 9.4l.8-1.9-1.2-2.1 2.8-2.8 2.1 1.2 1.9-.8L10 2Z" stroke="currentColor" stroke-width="1.5"/><circle cx="12" cy="12" r="3" stroke="currentColor" stroke-width="1.5"/></svg></button>')
    .replace('>🔔</button>', '><svg class="quick-icon" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M5 17h14l-2-3V9a5 5 0 0 0-10 0v5l-2 3Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M10 20a2 2 0 0 0 4 0" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg></button>');
};

renderHistory = function () {
  let list = [...state.events].sort((a,b) => b.date.localeCompare(a.date) || a.time.localeCompare(b.time));
  const query = state.historyQuery.trim().toLocaleLowerCase('ru');
  if (query) list = list.filter(event => event.title.toLocaleLowerCase('ru').includes(query));
  if (state.historyRange.from) list = list.filter(event => event.date >= state.historyRange.from);
  if (state.historyRange.to) list = list.filter(event => event.date <= state.historyRange.to);
  if (state.historyChat) list = list.filter(event => event.chat === state.historyChat);
  const chats = [...new Set(state.events.map(event => event.chat).filter(Boolean))].sort((a,b) => a.localeCompare(b,'ru'));
  const filter = `<div class="filter-popover filter-popover-expanded" role="group" aria-label="Фильтр событий">
    <strong>Фильтр</strong>
    <label>С даты<input type="date" id="history-from" value="${esc(state.historyRange.from)}"></label>
    <label>По дату<input type="date" id="history-to" value="${esc(state.historyRange.to)}"></label>
    <label>Чат<select id="history-chat"><option value="">Все чаты</option>${chats.map(chat => `<option value="${esc(chat)}" ${chat === state.historyChat ? 'selected' : ''}>${esc(chat)}</option>`).join('')}</select></label>
    <button class="filter-reset" data-action="reset-filters">Сбросить</button>
  </div>`;
  return `${header()}<main class="screen with-tabs"><h1 class="page-title">История</h1>
    <label class="searchbar">${icons.search}<input id="history-search" placeholder="Введите название события" value="${esc(state.historyQuery)}" aria-label="Поиск событий"></label>
    <div class="filter-row">${state.filterOpen ? filter : ''}<button class="filter-button" data-action="filter" aria-label="Фильтр">${icons.filter}</button></div>
    <p class="history-date">${state.historyRange.from || state.historyRange.to || state.historyChat ? `Найдено событий: ${list.length}` : dateLabel(state.selectedDate).replace(',', ' ·')}</p>
    <div class="history-list">${list.map(event => `<button class="card history-card clickable" data-event="${esc(event.id)}"><span class="card-title">${esc(event.title)}</span><span class="muted">${esc(event.time)} - ${esc(event.end)}<br>${esc(event.repeat === 'Не повторяется' ? event.status : event.repeat)} · ${esc(event.chat || 'Без чата')}</span><span class="chevron">›</span></button>`).join('') || '<div class="card empty">События не найдены</div>'}</div>
  </main>${tabs('history')}${footer()}`;
};

renderEvent = function () {
  const event = state.events.find(item => item.id === state.selectedEventId);
  if (!event) return `${header(false)}<main class="screen">${subhead('', 'Карточка события', 'history')}<div class="card empty">Событие не найдено</div></main>${footer()}`;
  const link = event.html_link || event.googleUrl || '';
  const validLink = /^https:\/\/calendar\.google\.com\//i.test(link);
  const recurring = event.repeat && event.repeat !== 'Не повторяется' && event.repeat !== 'Серия отменена';
  return `${header(false)}<main class="screen">${subhead('', 'Карточка события', state.returnTo || 'history')}
    <div class="event-details"><h2>${esc(event.title)}</h2>${field('Дата', dateLabel(event.date, true))}${field('Время', `${event.time} – ${event.end}`)}${field('Повторение', event.repeat || 'Не повторяется')}${field('Источник', event.source || 'Не указан')}${field('Чат', event.chat || 'Не указан')}${field('Синхронизация', event.sync || 'Не синхронизировано')}</div>
    <div class="event-actions"><button class="btn wide" data-action="open-event-google" ${validLink ? '' : 'disabled'}>${validLink ? 'Открыть в Google Calendar' : 'Ссылка на событие недоступна'}</button>
      <button class="btn soft wide" data-nav="event-edit">Редактировать</button>
      ${recurring ? '<button class="btn soft wide" data-action="cancel-series">Отменить серию</button>' : ''}
      <button class="btn soft wide" data-action="delete-event">Удалить</button></div>
  </main>${footer()}`;
};

function renderEventEdit() {
  const event = state.events.find(item => item.id === state.selectedEventId);
  if (!event) return renderEvent();
  return `${header(false)}<main class="screen">${subhead('✎', 'Редактировать событие', 'event')}
    <form id="event-edit-form" class="form">
      <label>Название<input class="form-input" name="title" required maxlength="120" value="${esc(event.title)}"></label>
      <label>Дата<input class="form-input" type="date" name="date" required value="${esc(event.date)}"></label>
      <div class="btn-row"><label>Начало<input class="form-input" type="time" name="time" required value="${esc(event.time)}"></label><label>Конец<input class="form-input" type="time" name="end" required value="${esc(event.end)}"></label></div>
      <label>Повторение<select class="form-input" name="repeat">${['Не повторяется','Повтор каждый день','Повтор каждую неделю','Повтор каждый месяц'].map(value => `<option ${event.repeat === value ? 'selected' : ''}>${value}</option>`).join('')}</select></label>
      <button class="btn wide" type="submit">Сохранить изменения</button>
    </form>
  </main>${footer()}`;
}

renderChats = function () {
  const allowed = state.chats || [];
  if(!DEMO_MODE)return `${header(false)}<main class="screen">${subhead('💬', 'Доступ к чатам', 'settings')}<div class="card"><h2>Разрешения Telegram Business</h2><p class="chat-note">Список чатов и изменение разрешений пока не предоставлены API бота. Настройте доступ в Telegram → Настройки → Telegram Business → Чат-боты.</p></div></main>${footer()}`;
  return `${header(false)}<main class="screen">${subhead('💬', 'Доступ к чатам', 'settings')}
    <p class="chat-note">${tg?.initData ? 'Статус доступа будет подтверждён после подключения API бота.' : 'Демо-данные. Реальные разрешения Telegram Business здесь пока не загружаются.'}</p>
    <div class="chat-group-label">ТИПЫ ЧАТОВ</div><div class="chat-panel">${[['Контакты','#00afb9','♙'],['Не контакты','#fed9b7','♧'],['Группы','#0081a7','♟']].map(([name,color,symbol]) => `<div class="chat-row"><span class="avatar" style="background:${color}">${symbol}</span><span>${name}</span><span class="access-mark">${name === 'Группы' ? '○' : '●'}</span></div>`).join('')}</div>
    <div class="chat-group-label">ЧАТЫ</div><div class="chat-panel">${['Галина','Аркадий','Анна','Ирина','Дарья','Татьяна','Михаил','Валерия'].map((chat,index) => `<div class="chat-row"><span class="avatar" style="background:${chatColors[index % chatColors.length]}"></span><span>${esc(chat)}</span>${allowed.includes(chat) ? `<button class="revoke-button" data-revoke="${esc(chat)}">Отозвать</button>` : '<span class="access-mark">○</span>'}</div>`).join('')}</div>
    <p class="chat-note">Чтобы изменить настоящие разрешения, откройте Telegram → Настройки → Telegram Business → Чат-боты.</p>
  </main>${footer()}`;
};

const demoRenderRecognized=renderRecognized;
renderRecognized=function(){return DEMO_MODE?demoRenderRecognized():`${header(false)}<main class="screen">${subhead('🔔','Распознанное событие','history')}<div class="card empty">Детали распознанного события API пока не предоставляет.</div></main>${footer()}`;};

const previousRender = render;
render = function () {
  if (!DEMO_MODE && state.apiStatus !== 'ready') {
    app.classList.toggle('dark', state.theme === 'dark' || state.theme === 'telegram' && tg?.colorScheme === 'dark');
    app.innerHTML = renderApiStatus();
    return;
  }
  if (screen === 'event-edit') {
    app.classList.toggle('dark', state.theme === 'dark' || state.theme === 'telegram' && tg?.colorScheme === 'dark');
    app.innerHTML = renderEventEdit() + renderModal();
  } else previousRender();
};

app.addEventListener('change', event => {
  const target = event.target;
  if (target.id === 'history-from') state.historyRange.from = target.value;
  else if (target.id === 'history-to') state.historyRange.to = target.value;
  else if (target.id === 'history-chat') state.historyChat = target.value;
  else return;
  persist(); render();
});

app.addEventListener('click', async event => {
  const reset = event.target.closest('[data-action="reset-filters"]');
  if (reset) { state.historyRange = { from:'', to:'' }; state.historyChat = ''; persist(); render(); return; }
  const google = event.target.closest('[data-action="open-event-google"]');
  if (google) {
    const item = state.events.find(value => value.id === state.selectedEventId);
    const url = item?.html_link || item?.googleUrl || '';
    if (!/^https:\/\/calendar\.google\.com\//i.test(url)) return;
    if (tg?.openLink) tg.openLink(url); else window.open(url, '_blank', 'noopener,noreferrer');
    return;
  }
  const cancel = event.target.closest('[data-action="cancel-series"]');
  if (cancel) { modal = {title:'Отменить всю серию?',text:'Повторяющееся событие и его будущие повторения будут удалены.',action:'confirm-cancel-series',button:'Отменить серию'}; render(); return; }
  const confirm = event.target.closest('[data-action="confirm-cancel-series"]');
  if (confirm) {
    const item = state.events.find(value => value.id === state.selectedEventId);
    const series = item?.seriesId || item?.id;
    if(!DEMO_MODE){try{await DealenderApi.cancelSeries(item.id);state.events=state.events.filter(value=>(value.seriesId||value.id)!==series);modal=null;navigate('history');feedback('Серия отменена')}catch(error){feedback(error.message)}return}
    state.events = state.events.filter(value => (value.seriesId || value.id) !== series);
    persist(); navigate('history'); feedback('Серия отменена'); return;
  }
  const revoke = event.target.closest('[data-revoke]');
  if (revoke) {
    modal = {title:'Отозвать доступ к чату',text:`Для чата «${revoke.dataset.revoke}» откройте Telegram → Настройки → Telegram Business → Чат-боты и измените разрешения бота. Mini App не может отозвать это разрешение самостоятельно.`,action:'modal-close',button:'Понятно'};
    render();
  }
});

app.addEventListener('submit', async event => {
  if (event.target.id !== 'event-edit-form') return;
  event.preventDefault();
  const data = new FormData(event.target);
  const item = state.events.find(value => value.id === state.selectedEventId);
  if (!item) return;
  const start = String(data.get('time'));
  const end = String(data.get('end'));
  if (end <= start) { feedback('Конец события должен быть позже начала'); return; }
  const patch={title:String(data.get('title')).trim(),date:String(data.get('date')),time:start,end,repeat:String(data.get('repeat'))};
  if(!DEMO_MODE){try{const result=await DealenderApi.updateEvent(item.id,patch);Object.assign(item,eventFromApi(result?.event||{...item,...patch}))}catch(error){feedback(error.message);return}}
  else Object.assign(item,patch);
  persist(); navigate('event'); feedback('Событие обновлено');
});

try { tg?.onEvent?.('themeChanged', () => render()); } catch (_) {}
if(DEMO_MODE)render();else loadIntegrationData();
