"""Responsive V7 Tk dashboard. The Canvas is scrolling only, never a preview."""
import math

# Unslop direction: a quiet instrument panel, not a promotional dashboard.
# Workflow inspired by unslop by Damir Sokolovsky (CC BY 4.0); see UI-DESIGN.md.
PALETTE = dict(background='#141415', panel='#1C1C1F', field='#252528',
               primary='#EDEDED', secondary='#B3B3BA', muted='#96969F',
               accent='#70D3C2', accent_ink='#10231F', border='#37373D',
               warning='#E8BC7B', error='#FFADB4')


def window_size(width, height, scale=1.):
    if not math.isfinite(scale) or not .5 <= scale <= 4:
        raise ValueError('Invalid display scale')
    if not all(math.isfinite(v) and v > 0 for v in (width, height)):
        raise ValueError('Invalid work area')
    return (round(min(960*scale, max(1, width-60*scale))),
            round(min(760*scale, max(1, height-80*scale))))


def column_count(width, scale=1.):
    return 2 if width / scale >= 760 else 1


def follow_footer_text(control_status, parameter_status):
    """Live control state wins over stale parameter-application notices."""
    live = control_status or '跟随状态：尚未初始化'
    if parameter_status.startswith(('待应用', '未应用')):
        return live + '\n' + parameter_status
    return live


def build_ui(app, root, config):
    import tkinter as tk
    from tkinter import ttk
    from strength_profiles import STRENGTH_PROFILES
    from ui_settings import FIELDS, outline_color_label
    from outline_settings import OUTLINE_COLORS

    app.prepare_adaptation()
    app.ui_config = config
    bg, panel, field, ink, muted, accent = (PALETTE[k] for k in ('background','panel','field','primary','muted','accent'))
    scale = max(1., min(4., root.winfo_fpixels('1i') / 96.))
    px = lambda value: round(value * scale)
    size = window_size(root.winfo_screenwidth(), root.winfo_screenheight(), scale)
    root.geometry(f'{size[0]}x{size[1]}')
    root.minsize(min(px(500), size[0]), min(px(460), size[1]))
    root.configure(bg=bg)
    style = ttk.Style(root)
    style.theme_use('clam')
    style.configure('.', font=('Microsoft YaHei UI', 10), borderwidth=0)
    style.configure('TFrame', background=panel)
    style.configure('App.TFrame', background=bg)
    style.configure('TLabel', background=panel, foreground=ink)
    style.configure('Muted.TLabel', foreground=PALETTE['secondary'], font=('Microsoft YaHei UI', 9))
    style.configure('Tiny.TLabel', foreground=muted, font=('Microsoft YaHei UI', 9))
    style.configure('App.TLabel', background=bg, foreground=muted, font=('Microsoft YaHei UI', 9))
    style.configure('Error.TLabel', foreground=PALETTE['error'], font=('Microsoft YaHei UI', 9))
    style.configure('Warning.TLabel', foreground=PALETTE['warning'], font=('Microsoft YaHei UI', 9))
    style.configure('Title.TLabel', background=bg, foreground=ink, font=('Microsoft YaHei UI', 18))
    style.configure('Section.TLabel', font=('Microsoft YaHei UI', 11, 'bold'))
    style.configure('Badge.TLabel', foreground=accent, font=('Microsoft YaHei UI', 9))
    style.configure('TButton', background='#2B2B30', foreground=ink, padding=(px(12), px(8)),
                    relief='flat', borderwidth=1, bordercolor='#2B2B30', focuscolor=accent)
    style.map('TButton', background=[('active', '#38383F'), ('disabled', '#232326')],
              foreground=[('disabled', muted)], bordercolor=[('focus', accent)])
    style.configure('Accent.TButton', background=ink, foreground=bg, bordercolor=ink)
    style.map('Accent.TButton', background=[('active', '#FFFFFF'), ('disabled', '#303034')],
              foreground=[('disabled', muted), ('!disabled', bg)])
    style.configure('Chip.TButton', padding=(px(8), px(8)), background='#27272B')
    style.configure('Selected.TButton', padding=(px(8), px(8)), background=accent,
                    foreground=PALETTE['accent_ink'], bordercolor=accent)
    style.map('Selected.TButton', background=[('active', '#96E0D3')], foreground=[('!disabled', PALETTE['accent_ink'])])
    style.configure('Nav.TButton', padding=(px(12), px(8)), background=bg, bordercolor=bg, foreground=muted)
    style.configure('NavSelected.TButton', padding=(px(12), px(8)), background='#303034', bordercolor='#303034', foreground=ink)
    style.configure('Step.TButton', padding=(px(4), px(4)))
    style.configure('TEntry', fieldbackground=field, foreground=ink, insertcolor=ink,
                    padding=(px(8), px(4)), bordercolor=PALETTE['border'], lightcolor=PALETTE['border'], darkcolor=PALETTE['border'])
    style.map('TEntry', foreground=[('disabled', muted)], fieldbackground=[('disabled', '#222225')], bordercolor=[('focus', accent)])
    style.configure('TCombobox', fieldbackground=field, foreground=ink, background='#303034',
                    padding=px(4), arrowcolor=muted, bordercolor=PALETTE['border'])
    style.map('TCombobox', fieldbackground=[('readonly', field)], foreground=[('disabled', muted), ('readonly', ink)], bordercolor=[('focus', accent)])
    root.option_add('*TCombobox*Listbox.background', field)
    root.option_add('*TCombobox*Listbox.foreground', ink)
    root.option_add('*TCombobox*Listbox.selectBackground', '#315A51')
    root.option_add('*TCombobox*Listbox.selectForeground', ink)
    style.configure('TCheckbutton', background=panel, foreground=ink, padding=(0, px(4)), focuscolor=accent)
    style.map('TCheckbutton', background=[('active', panel)], indicatorbackground=[('selected', accent), ('!selected', field)])
    style.configure('Vertical.TScrollbar', background='#3A3A40', troughcolor=bg, arrowsize=px(12), borderwidth=0)
    style.configure('TSeparator', background=PALETTE['border'])

    app.ui_sections, app.ui_fields, app.ui_errors, app.ui_field_widgets = {}, {}, {}, {}
    app.strength_buttons = {}
    app._ui_syncing = True

    def variable(name, value='', boolean=False):
        value = (tk.BooleanVar if boolean else tk.StringVar)(master=root, value=value)
        setattr(app, name, value)
        return value

    for name in ('status', 'control_status', 'backend_status', 'diagnostic_status', 'performance_status',
                 'results', 'target_filter_status', 'outline_compatibility_status', 'outline_current_status', 'prediction_status', 'input_diagnostic_status',
                 'capture_settings_status', 'motion_save_status', 'fire_lock_status', 'strength_description', 'adapt_summary',
                 'adapt_error', 'strength_choice', 'strength_badge', 'ui_commit_status', 'tolerance_hint',
                 'runtime_heading', 'runtime_hint', 'draft_change_summary', 'gain_hint', 'recognition_badge', 'follow_badge', 'footer_action_status', 'strength_impact_badge', 'strength_cost_detail'):
        variable(name)
    variable('follow_var', False, True)
    variable('trigger_var',False,True)
    variable('trigger_status','默认关闭 · 不保存开启状态')
    variable('trigger_badge','自动扳机：关闭')
    from aim_region import AIM_REGIONS
    variable('aim_region_var', AIM_REGIONS[app.follower.aim_region])
    variable('enemy_color_var', outline_color_label(getattr(app, 'enemy_color', 'red')))
    variable('rhythm_var', app.follower.pacing_enabled, True)
    variable('soft_mode_var', app.follower.soft_assist, True)
    variable('manual_fire_priority_var', app.follower.manual_fire_priority, True)
    variable('fire_target_lock_var', app.follower.fire_target_lock, True)
    variable('dpi_var', str(app.adaptation_inputs['dpi']))
    variable('sensitivity_var', f"{app.adaptation_inputs['sensitivity']:g}")
    for name in ('gain_mode_var', 'algorithm_var', 'capture_var', 'inference_var'):
        variable(name)

    outer = ttk.Frame(root, style='App.TFrame', padding=px(20))
    outer.pack(fill='both', expand=True)
    header = ttk.Frame(outer, style='App.TFrame')
    header.pack(fill='x', pady=(0, px(12)))
    identity = ttk.Frame(header, style='App.TFrame')
    identity.pack(side='left', fill='x', expand=True)
    ttk.Label(identity, text='V7 控制台', style='Title.TLabel').pack(anchor='w')
    ttk.Label(identity, textvariable=app.runtime_heading, style='App.TLabel').pack(anchor='w', pady=(px(4),0))
    badges = ttk.Frame(header, style='App.TFrame')
    badges.pack(side='right')
    ttk.Label(badges, textvariable=app.recognition_badge, style='App.TLabel', anchor='e').pack(fill='x')
    ttk.Label(badges, textvariable=app.trigger_badge, style='App.TLabel', anchor='e').pack(fill='x')

    def wrapped(parent, *, text=None, var=None, style_name='Muted.TLabel'):
        widget = ttk.Label(parent, text=text, textvariable=var, style=style_name, justify='left', anchor='w')
        widget.bind('<Configure>', lambda e: widget.configure(wraplength=max(px(80), e.width)))
        return widget

    # The footer is packed BEFORE the viewport so stop/follow remain accessible.
    footer = ttk.Frame(outer, style='App.TFrame')
    footer.pack(side='bottom', fill='x', pady=(px(12), 0))
    actions = ttk.Frame(footer, style='App.TFrame')
    actions.pack(fill='x')
    app.start_button = ttk.Button(actions, text='开始识别', command=app.start_live, style='Accent.TButton')
    app.stop_button = ttk.Button(actions, text='停止识别', command=app.stop)
    app.ui_apply_button = ttk.Button(actions, text='应用全部参数', command=app.commit_ui_parameters)
    app.ui_revert_button = ttk.Button(actions, text='放弃编辑', command=app.revert_ui_drafts)
    app.follow_toggle = ttk.Checkbutton(actions, text='准心跟随 · F8', variable=app.follow_var, command=app.toggle_follow)
    app.start_button.grid(row=0, column=0, padx=(0, px(8)), sticky='w')
    app.stop_button.grid(row=0, column=1, padx=(0, px(8)), sticky='w')
    app.ui_apply_button.grid(row=0, column=2, sticky='w')
    app.ui_revert_button.grid(row=0, column=3, padx=(px(8),0), sticky='w')
    actions.columnconfigure(4, weight=1)
    app.follow_toggle.grid(row=0, column=4, sticky='e')
    wrapped(footer, var=app.runtime_hint, style_name='App.TLabel').pack(fill='x', pady=(px(8), 0))
    wrapped(footer, var=app.status, style_name='App.TLabel').pack(fill='x', pady=(px(4), 0))
    footer_error = wrapped(footer, var=app.adapt_error, style_name='Error.TLabel')
    def footer_error_changed(*args):
        if app.adapt_error.get():
            footer_error.pack(fill='x', pady=(px(4), 0))
        else:
            footer_error.pack_forget()
    app.adapt_error.trace_add('write', footer_error_changed)
    app.follow_status_label = wrapped(footer, var=app.footer_action_status, style_name='App.TLabel')
    app.follow_status_label.pack(fill='x', pady=(px(4), 0))
    def footer_action_changed(*args):
        app.footer_action_status.set(follow_footer_text(app.control_status.get(), app.ui_commit_status.get()))
    app.control_status.trace_add('write', footer_action_changed)
    app.ui_commit_status.trace_add('write', footer_action_changed)

    navigation = ttk.Frame(outer, style='App.TFrame')
    navigation.pack(fill='x', pady=(0,px(8)))
    app.ui_nav_buttons = {}
    for index, (key,title,attr) in enumerate((('common','常用参数','ui_main_button'),
            ('advanced','高级设置','ui_advanced_button'),('details','运行详情','ui_details_button'))):
        button=ttk.Button(navigation,text=title,style='Nav.TButton',command=lambda k=key:app.set_ui_page(k))
        button.grid(row=0,column=index,sticky='ew',padx=(0,px(4)))
        navigation.columnconfigure(index,weight=1)
        app.ui_nav_buttons[key]=button;setattr(app,attr,button)
    commit_note=ttk.Frame(outer,style='App.TFrame')
    commit_note.pack(fill='x',pady=(0,px(8)))
    wrapped(commit_note,var=app.draft_change_summary,style_name='App.TLabel').pack(side='left',fill='x',expand=True)
    app.ui_cost_button=ttk.Button(commit_note,text='查看力度与响应代价',style='Step.TButton',command=lambda:show_costs())
    app.ui_cost_button.pack(side='right',padx=(px(8),0))

    viewport = ttk.Frame(outer, style='App.TFrame')
    viewport.pack(fill='both', expand=True)
    scrollbar = ttk.Scrollbar(viewport, orient='vertical')
    scrollbar.pack(side='right', fill='y')
    canvas = tk.Canvas(viewport, bg=bg, highlightthickness=0, yscrollcommand=scrollbar.set)
    canvas.pack(side='left', fill='both', expand=True)
    scrollbar.configure(command=canvas.yview)
    content = ttk.Frame(canvas, style='App.TFrame')
    item = canvas.create_window((0, 0), window=content, anchor='nw')
    app.ui_scroll_canvas, app.ui_content = canvas, content
    content.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))

    def wheel(event):
        if canvas.bbox('all') and canvas.bbox('all')[3] > canvas.winfo_height():
            canvas.yview_scroll(-1 if event.delta > 0 else 1, 'units')
        return 'break'
    root.bind('<MouseWheel>', wheel)

    def draft_keys(widget):
        widget.bind('<Return>', app.commit_ui_parameters)
        widget.bind('<Escape>', app.revert_ui_drafts)
        widget.bind('<MouseWheel>', wheel)
        return widget

    def card(parent, key, title):
        frame = ttk.Frame(parent, padding=px(16))
        app.ui_sections[key] = frame
        heading = ttk.Frame(frame)
        heading.pack(fill='x', pady=(0, px(12)))
        ttk.Label(heading, text=title, style='Section.TLabel').pack(side='left')
        frame.ui_heading = heading
        return frame

    common=ttk.Frame(content,style='App.TFrame')
    common.pack(fill='x')
    strength = card(common, 'strength', '辅助强度')
    strength.pack(fill='x', pady=(0, px(12)))
    app.strength_impact_label = ttk.Label(strength.ui_heading, textvariable=app.strength_impact_badge, foreground=PALETTE['warning'])
    app.strength_impact_label.pack(side='right')
    wrapped(strength.ui_heading,text='立即应用 · 暂停跟随并重置其他草稿',style_name='Tiny.TLabel').pack(side='left',fill='x',expand=True,padx=(px(12),px(12)))
    chips = ttk.Frame(strength)
    chips.pack(fill='x')
    for i, preset in enumerate(STRENGTH_PROFILES):
        button = ttk.Button(chips, text=f'{preset.level}  {preset.name}', style='Chip.TButton',
                            command=lambda level=preset.level: app.apply_strength_level(level))
        button.grid(row=0, column=i, sticky='ew', padx=(0 if i == 0 else px(8), 0))
        chips.columnconfigure(i, weight=1, uniform='chips')
        app.strength_buttons[preset.level] = button
    strength_meta = ttk.Frame(strength)
    strength_meta.pack(fill='x', pady=(px(12), 0))
    app.restore_strength_button = ttk.Button(strength_meta, text='恢复本档', command=app.restore_basis_strength, style='Step.TButton')
    app.restore_strength_button.pack(side='right')
    ttk.Label(strength_meta, textvariable=app.strength_badge, style='Badge.TLabel').pack(side='right', padx=px(12))
    wrapped(strength_meta, var=app.strength_description).pack(side='left', fill='x', expand=True)

    columns = ttk.Frame(common, style='App.TFrame')
    columns.pack(fill='x')
    columns.columnconfigure(0, weight=1, uniform='main')
    columns.columnconfigure(1, weight=1, uniform='main')
    adapt = card(columns, 'adaptation', '灵敏度适配')
    tuning = card(columns, 'tuning', '核心调参')
    adapt.grid(row=0, column=0, sticky='nsew', padx=(0, px(8)))
    tuning.grid(row=0, column=1, sticky='nsew', padx=(px(8), 0))
    for title, attr, cell in (('鼠标 DPI', 'dpi_entry', app.dpi_var), ('游戏基础灵敏度', 'sensitivity_entry', app.sensitivity_var)):
        row = ttk.Frame(adapt)
        row.pack(fill='x', pady=(0, px(8)))
        ttk.Label(row, text=title).pack(side='left')
        entry = ttk.Entry(row, textvariable=cell, width=13)
        entry.pack(side='right')
        entry.bind('<Return>', app.commit_ui_parameters)
        entry.bind('<Escape>', app.revert_ui_drafts)
        setattr(app, attr, entry)
    ttk.Label(adapt, text='显示器', style='Muted.TLabel').pack(anchor='w', pady=(px(4), px(4)))
    app.monitor = ttk.Combobox(adapt, values=app.monitors, state='readonly', width=12)
    app.monitor.pack(fill='x')
    if app.monitors:
        app.monitor.current(0)
    app.monitor.bind('<<ComboboxSelected>>', app.change_monitor)
    draft_keys(app.monitor)
    wrapped(adapt, var=app.adapt_summary).pack(fill='x', pady=(px(16), px(8)))
    wrapped(adapt, text='不开镜估算 · 基准 800 DPI / 0.3\nDPI 不会重复乘入相对位移。', style_name='Tiny.TLabel').pack(fill='x')
    app.adapt_apply_button = ttk.Button(adapt, text='应用全部参数', command=app.commit_ui_parameters)
    app.adapt_apply_button.pack(anchor='w', pady=(px(12), 0))

    app.ui_field_rows={}
    def numeric(parent, key, unit):
        title, spec = FIELDS[key]
        row = ttk.Frame(parent)
        row.pack(fill='x', pady=(0, px(8)))
        row.columnconfigure(0, weight=1)
        ttk.Label(row, text=title).grid(row=0, column=0, sticky='w')
        var = tk.StringVar(master=root)
        app.ui_fields[key] = var
        error = tk.StringVar(master=root)
        app.ui_errors[key] = error
        controls = ttk.Frame(row)
        controls.grid(row=0, column=1, rowspan=2, sticky='e')
        minus = ttk.Button(controls, text='−', width=2, style='Step.TButton', command=lambda: app.step_ui_field(key, -1))
        entry = ttk.Entry(controls, textvariable=var, width=7, justify='center')
        plus = ttk.Button(controls, text='+', width=2, style='Step.TButton', command=lambda: app.step_ui_field(key, 1))
        minus.pack(side='left')
        entry.pack(side='left', padx=px(4))
        plus.pack(side='left')
        app.ui_field_widgets[key] = (minus, entry, plus)
        app.ui_field_rows[key]=row
        entry.bind('<Return>', app.commit_ui_parameters)
        entry.bind('<Escape>', app.revert_ui_drafts)
        ttk.Label(row, text=f'{spec.range_hint} {unit}', style='Tiny.TLabel').grid(row=1, column=0, sticky='w')
        err_label = wrapped(row, var=error, style_name='Error.TLabel')
        def show_error(*args):
            if error.get():
                err_label.grid(row=2, column=0, columnspan=2, sticky='ew')
                if hasattr(app,'set_ui_page') and not app._closing:
                    app.set_ui_page('common' if key in ('gain','response_ms','chase_strength','base_limit','tolerance_px') else 'advanced')
                    root.after_idle(lambda: scroll_to(row))
            else:
                err_label.grid_remove()
        error.trace_add('write', show_error)
        var.trace_add('write', lambda *args: app.mark_ui_draft(key))
        return entry

    numeric(tuning, 'gain', '×')
    gain_row = ttk.Frame(tuning.ui_heading)
    gain_row.pack(side='right')
    ttk.Label(gain_row, textvariable=app.gain_mode_var, style='Badge.TLabel').pack(side='left', padx=(0, px(8)))
    ttk.Button(gain_row, text='恢复自动', style='Step.TButton', command=app.restore_auto_gain).pack(side='right')
    for key, unit in (('response_ms', 'ms'), ('chase_strength', '×'), ('base_limit', '计数'), ('tolerance_px', 'px')):
        numeric(tuning, key, unit)
    wrapped(tuning, var=app.tolerance_hint, style_name='Tiny.TLabel').pack(fill='x')

    # Group-wide errors remain visible even when advanced/details are collapsed.
    app.adapt_error_label = wrapped(adapt, var=app.adapt_error, style_name='Error.TLabel')
    def show_error(*args):
        if app.adapt_error.get():
            app.adapt_error_label.pack(fill='x', pady=(px(8), 0))
        else:
            app.adapt_error_label.pack_forget()
    app.adapt_error.trace_add('write', show_error)

    advanced = card(content, 'advanced', '高级设置')
    details = card(content, 'details', '运行详情')
    app.ui_pages={'common':common,'advanced':advanced,'details':details}
    app.ui_current_page='common'
    scroll_positions={key:0. for key in app.ui_pages}
    def set_page(key):
        if key not in app.ui_pages:raise ValueError('Unknown UI page')
        if getattr(app,'_closing',False):return False
        previous=app.ui_current_page
        if previous==key and app.ui_pages[key].winfo_manager():return True
        scroll_positions[previous]=canvas.yview()[0]
        for name,frame in app.ui_pages.items():
            frame.pack_forget()
            app.ui_nav_buttons[name].configure(style='NavSelected.TButton' if name==key else 'Nav.TButton')
        app.ui_pages[key].pack(fill='x')
        app.ui_current_page=key
        def restore_scroll():
            if not app._closing and app.ui_current_page==key:
                canvas.configure(scrollregion=canvas.bbox('all'))
                canvas.yview_moveto(scroll_positions[key])
        root.after_idle(restore_scroll)
        return True
    app.set_ui_page=set_page
    app.ui_nav_buttons['common'].configure(style='NavSelected.TButton')
    for number,key in enumerate(app.ui_pages,1):
        root.bind('<Alt-Key-'+str(number)+'>',lambda e,k=key:(set_page(k),'break')[1])
    def scroll_to(widget):
        if app._closing or not widget.winfo_exists():return
        root.update_idletasks()
        y=widget.winfo_rooty()-content.winfo_rooty()
        canvas.yview_moveto(max(0.,(y-px(8))/max(1,content.winfo_height())))
    def show_costs():
        set_page('advanced')
        root.after_idle(lambda:scroll_to(cost_group))
    def group(parent,title,note=None):
        frame=ttk.Frame(parent)
        frame.pack(fill='x',pady=(px(8),px(16)))
        ttk.Separator(frame).pack(fill='x',pady=(0,px(12)))
        ttk.Label(frame,text=title,style='Section.TLabel').pack(anchor='w',pady=(0,px(8)))
        if note:wrapped(frame,text=note).pack(fill='x',pady=(0,px(8)))
        return frame
    def disclosure(parent,title,text):
        button=ttk.Button(parent,text=title+' +',style='Step.TButton')
        button.pack(anchor='w',pady=(px(4),px(4)))
        body=wrapped(parent,text=text,style_name='Tiny.TLabel')
        def toggle():
            if body.winfo_manager():body.pack_forget();button.configure(text=title+' +')
            else:body.pack(fill='x',after=button,pady=(0,px(8)));button.configure(text=title+' −')
        button.configure(command=toggle)
        return body
    wrapped(advanced,text='以下参数先编辑、后应用。有效变更会关闭跟随和扳机，需手动重新开启。').pack(fill='x')
    target_group=group(advanced,'目标与识别','部位和置信度可编辑；画面范围与输入尺寸须停止识别后应用')
    aim_row=ttk.Frame(target_group);aim_row.pack(fill='x',pady=(0,px(8)))
    ttk.Label(aim_row,text='定位部位').pack(side='left')
    app.aim_region_choice=draft_keys(ttk.Combobox(aim_row,textvariable=app.aim_region_var,values=tuple(AIM_REGIONS.values()),state='readonly',width=23))
    app.aim_region_choice.pack(side='right')
    wrapped(target_group,text='胸部按联合验证的身体框估算；只有头框时暂停，不推测胸部',style_name='Tiny.TLabel').pack(fill='x',pady=(0,px(8)))
    color_row=ttk.Frame(target_group);color_row.pack(fill='x',pady=px(4))
    ttk.Label(color_row,text='敌方轮廓颜色').pack(side='left')
    app.enemy_color_choice=draft_keys(ttk.Combobox(color_row,textvariable=app.enemy_color_var,values=tuple(OUTLINE_COLORS.values()),state='readonly',width=23))
    app.enemy_color_choice.pack(side='right')
    wrapped(target_group,var=app.outline_compatibility_status,style_name='Warning.TLabel').pack(fill='x',pady=(px(4),px(8)))
    numeric(target_group,'confidence','')
    app.ui_draft_choices={'aim_region':app.aim_region_choice,'enemy_color':app.enemy_color_choice}
    for key,title,var,values in (('capture','画面范围',app.capture_var,tuple(config['capture_scopes'].values())),
            ('inference','识别输入尺寸',app.inference_var,tuple(map(str,config['inference_sizes'])))):
        row=ttk.Frame(target_group);row.pack(fill='x',pady=px(4))
        ttk.Label(row,text=title).pack(side='left')
        choice=draft_keys(ttk.Combobox(row,textvariable=var,values=values,state='readonly',width=18))
        choice.pack(side='right');app.ui_draft_choices[key]=choice
    control_group=group(advanced,'控制算法','改变模式只改草稿，不改档位和数值；预测强锁的实际输出仍可能增减')
    row=ttk.Frame(control_group);row.pack(fill='x',pady=(0,px(8)))
    ttk.Label(row,text='控制模式').pack(side='left')
    choice=draft_keys(ttk.Combobox(row,textvariable=app.algorithm_var,values=tuple(config['algorithms']),state='readonly',width=20))
    choice.pack(side='right');app.ui_draft_choices['algorithm']=choice
    algorithm_actions=ttk.Frame(control_group);algorithm_actions.pack(fill='x',pady=(0,px(8)))
    app.preview_lock_button=ttk.Button(algorithm_actions,text='预览强锁',command=lambda:app.preview_control_algorithm('lock'),style='Step.TButton')
    app.preview_legacy_button=ttk.Button(algorithm_actions,text='预览兼容',command=lambda:app.preview_control_algorithm('legacy'),style='Step.TButton')
    app.preview_lock_button.pack(side='left',padx=(0,px(8)));app.preview_legacy_button.pack(side='left')
    numeric(control_group,'pd_kp','');numeric(control_group,'pd_kd','')
    wrapped(control_group,text='P / D 仅在 PD 模式生效；不是完整 PID。预测强锁为实验模式，不保证实战更强。',style_name='Tiny.TLabel').pack(fill='x')
    coordination=group(advanced,'跟随协调','可选策略不会因界面升级自动开启；每项代价如下')
    def option(attr,title,var,cost):
        toggle=ttk.Checkbutton(coordination,text=title,variable=var)
        toggle.pack(anchor='w');setattr(app,attr,toggle)
        wrapped(coordination,text=cost,style_name='Warning.TLabel').pack(fill='x',pady=(0,px(8)))
    option('soft_toggle','柔和容差',app.soft_mode_var,'降低近中心力度，允许误差范围内停止')
    option('rhythm_toggle','均匀细调',app.rhythm_var,'延迟代价：小修正分摊到最多 3 ms；中断时不补发尾部')
    option('manual_priority_toggle','开火手动优先 · 降低该轴力度',app.manual_fire_priority_var,'手动移动的轴优先，辅助可降至 0；停手后渐恢复')
    option('fire_target_lock_toggle','开火保留当前目标',app.fire_target_lock_var,'丢检、身份不明或预算不足时暂停；可能延后换敌，不能判断击杀')
    disclosure(coordination,'保留目标的工作方式','开火期间及松开后 350 ms 保留原目标；无法可靠匹配时暂停而非换人。松开超过保护窗口恢复原切换规则，F8 关闭再开启可重新选择。超过 8 px 的异常跳变可能重置速度/滤波状态，瞬时输出会变化；匹配增加 CPU 工作，不保证 FPS 不降。默认关闭，需勾选并应用，保存的选择优先。')
    cost_group=group(advanced,'力度与响应代价','区分当前已生效策略、本次草稿与最近实际变更；参数数值不变不代表每帧等力度')
    app.strength_cost_label=wrapped(cost_group,var=app.strength_cost_detail,style_name='Warning.TLabel')
    app.strength_cost_label.pack(fill='x')
    wrapped(cost_group,var=app.gain_hint).pack(fill='x',pady=(px(8),0))
    wrapped(details,text='这里显示已经生效的状态；修改参数请回常用参数或高级设置').pack(fill='x',pady=(0,px(8)))
    trigger_group=group(details,'运行开关 · 立即生效','此开关不属于参数草稿，也不保存开启状态')
    app.trigger_toggle=ttk.Checkbutton(trigger_group,text='自动扳机',variable=app.trigger_var,command=app.toggle_trigger)
    app.trigger_toggle.pack(anchor='w')
    wrapped(trigger_group,var=app.trigger_status).pack(fill='x')
    wrapped(trigger_group,text='仅虚拟 HID；需跟随开启、目标居中并通过确认。仍有报告 I/O 与调度开销；停止 / 失焦会提前松键。',style_name='Warning.TLabel').pack(fill='x',pady=(px(4),0))
    disclosure(trigger_group,'触发条件与时序','两帧确认、置信度至少 0.5；原定位验证头框，胸部验证身体框上部。按下目标保持 40 ms，并预留 150 ms 冷却，按下确认间隔至少 190 ms。识别和移动不等待整个保持时间；实际保持可能更长或因保护提前松开，故障时不保证时限。已开启的开火暂停 / 手动优先仍按原策略生效。')
    for title,variables in (('跟随与输入',(app.control_status,app.backend_status,app.input_diagnostic_status)),
            ('识别与筛选诊断',(app.outline_current_status,app.target_filter_status)),
            ('识别与性能',(app.performance_status,app.capture_settings_status)),
            ('模型与目标保留',(app.prediction_status,app.fire_lock_status)),
            ('配置与诊断',(app.motion_save_status,app.ui_commit_status,app.diagnostic_status))):
        frame=group(details,title)
        for var in variables:wrapped(frame,var=var).pack(fill='x',pady=(0,px(8)))
    disclosure(details,'诊断说明','无画面预览。识别与跟随相互独立；应用变更后不自动恢复跟随。算法仍保留全部 21 个候选评分，后台诊断可能延后，忙时只保留最新待写快照，不丢弃输入。性能数字不是游戏帧率保证。')

    last_layout = [None]
    def resize(event):
        canvas.itemconfigure(item, width=event.width)
        count = column_count(event.width, scale)
        if last_layout[0] != count:
            last_layout[0] = count
            if count == 2:
                adapt.grid(row=0, column=0, columnspan=1, sticky='nsew', padx=(0, px(8)), pady=0)
                tuning.grid(row=0, column=1, columnspan=1, sticky='nsew', padx=(px(8), 0), pady=0)
            else:
                adapt.grid(row=0, column=0, columnspan=2, sticky='nsew', padx=0, pady=(0, px(12)))
                tuning.grid(row=1, column=0, columnspan=2, sticky='nsew', padx=0, pady=0)
            for col in range(6):
                chips.columnconfigure(col, weight=1 if count == 2 or col < 3 else 0, uniform='chips' if count == 2 or col < 3 else '')
            for i, button in enumerate(app.strength_buttons.values()):
                button.grid(row=i//3 if count == 1 else 0, column=i%3 if count == 1 else i,
                            padx=(0 if i % (3 if count == 1 else 6) == 0 else px(8), 0), pady=(px(8) if count == 1 and i >= 3 else 0, 0))
            app.follow_toggle.grid(row=1 if count == 1 else 0, column=0 if count == 1 else 4,
                                   columnspan=4 if count == 1 else 1, sticky='w' if count == 1 else 'e',
                                   pady=(px(4) if count == 1 else 0, 0))
    canvas.bind('<Configure>', resize)

    def badges_changed(*args):
        from ui_presenter import workflow_status
        state=app.follower.snapshot()
        message=app.status.get()
        worker=getattr(app,'worker',None)
        running=bool(getattr(app,'live_mode',False) and worker is not None and worker.is_alive() and not app.stop_event.is_set())
        state['recognition_error']=getattr(app,'_run_error',None)
        view=workflow_status(state,running=running,loading=('加载' in message),
            stopping=bool(getattr(app,'_stopping',False)),closing=bool(getattr(app,'_closing',False)),
            recovering=bool(getattr(app,'_startup_input_error',None)),
            arm_pending=getattr(app,'_follow_arm_deadline',None) is not None,
            dirty=app.ui_commit_status.get().startswith(('待应用','未应用')),
            trigger_enabled=app._trigger.enabled)
        for cell,value in ((app.recognition_badge,view['recognition']), (app.follow_badge,view['follow']),
                (app.trigger_badge,view['follow']+' · '+view['trigger']),
                (app.runtime_heading,view['title']), (app.runtime_hint,view['hint'])):
            if cell.get()!=value:cell.set(value)
    app.ui_refresh_status=badges_changed
    app.ui_commit_status.trace_add('write',badges_changed)
    app.status.trace_add('write', badges_changed)
    app.control_status.trace_add('write', badges_changed)
    for var in (app.enemy_color_var, app.manual_fire_priority_var, app.fire_target_lock_var, app.aim_region_var, app.dpi_var, app.sensitivity_var, app.algorithm_var, app.capture_var, app.inference_var, app.soft_mode_var, app.rhythm_var):
        var.trace_add('write', app.mark_ui_draft)
    app._ui_syncing = False
    app.sync_ui_drafts()
    app.refresh_adaptation_readout()
    app._refresh_strength_profile()
    from recognition_diagnostics import format_recognition_report
    app.target_filter_status.set(format_recognition_report({}))
    app.status.set('尚未开始识别 · 未验证输入设备')
    app.control_status.set('默认关闭 · 开始识别后，手动或 F8 开启')
    app.motion_save_status.set('V7 独立配置 · 不修改 V5 / V6')
    app.ui_commit_status.set('回车应用整组参数 · Esc 放弃编辑')
    badges_changed()
