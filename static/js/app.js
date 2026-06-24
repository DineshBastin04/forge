function app() {
  return {
    user: null, view: 'device-reset', sidebarOpen: false, toasts: [], dbConfigs: [],
    loginForm: { username:'', password:'', error:'', loading:false },
    confirm: { show: false, msg: '', resolve: null },
    dark: true,
    settings: { default_theme: 'dark', saving: false },
    dr: { tab:'scheduler', schedulerInfo:null, schedulerLoading:false, intervalInput:'',
          manualDbId:'', manualDeviceId:'', manualInputType:'device', manualLoading:false, manualResult:null,
          logs:[], logsLoading:false, logSearch:'', logLevel:'',
          scanDbId:'', scanLoading:false, scanResults:[], execLoading:false, execResults:[] },
    up: { tab:'scheduler', schedulerInfo:null, schedulerLoading:false, intervalInput:'',
          scanDbId:'', scanLoading:false, scanResults:[], execLoading:false, execResults:[],
          manualDbId:'', manualWhId:'', manualOrderNum:'', manualItemNum:'', manualLoading:false, manualResult:null,
          partialDbId:'', partialWhId:'', partialOrderNum:'', partialItemNum:'', partialQty:'',
          partialLoading:false, partialResult:null, logs:[], logsLoading:false, logSearch:'', logLevel:'' },
    au: { list:[], loading:false, modal:false, editId:null, saving:false, userSearch:'',
          form:{username:'',display_name:'',password:'',role:'user',agent_perms:[],is_active:true},
          auditLogs:[], auditLoading:false, auditSearch:'', auditDetailModal:false, activeAudit:null },
    dbc: { list:[], loading:false, modal:false, editId:null, modalTab:'connection', saving:false,
           testConnLoading:false, testConnResult:null,
           formTestLoading:false, formTestResult:null,
           logFormTestLoading:false, logFormTestResult:null,
           form:{name:'',db_type:'mssql',
                 db:{server:'',port:'',database:'',username:'',password:'',driver:''},
                 use_log_db:false,
                 log_db:{server:'',port:'',database:'',username:'',password:'',driver:''},
                 notify:{teams_webhook:'',slack_webhook:'',report_after_run:false,on_error:true,on_warning:false}} },
    profile: { modal:false, saving:false, form:{display_name:'',current_password:'',new_password:''} },
    registeredAgents: [],
    myHistory: [],
    myHistoryLoading: false,
    myHistorySearch: '',
    aw: { list:[], loading:false, modal:false, editId:null, saving:false, error:'',
          form:{id:'', name:'', description:'', flow_yaml:''} },

    async init() {
      // 1. Fetch system default theme from settings endpoint
      try {
        const resSettings = await fetch('/api/v0/settings');
        if (resSettings.ok) {
          const s = await resSettings.json();
          this.settings.default_theme = s.default_theme || 'dark';
        }
      } catch (e) {}

      // 2. Resolve theme (strictly controlled by default_theme setting)
      this.dark = this.settings.default_theme === 'dark';
      this.applyTheme();

      // 3. User authentication & load dashboard
      try {
        const r = await fetch('/api/v0/auth/me',{credentials:'same-origin'});
        if (r.ok) {
          this.user = await r.json();
          await this.loadAgents();
          this.view = this._dv();
          await this.loadDbConfigs();
          this._lvd();
          if (this.user && this.user.force_change_password) {
            this.openProfile();
          }
        }
      } catch(e) {}
    },
    toggleTheme() {
      this.dark = !this.dark;
      localStorage.setItem('user-theme', this.dark ? 'dark' : 'light');
      this.applyTheme();
    },
    applyTheme() {
      if (this.dark) {
        document.documentElement.classList.add('dark');
      } else {
        document.documentElement.classList.remove('dark');
      }
      // Notify Three.js visual script of the change
      window.dispatchEvent(new CustomEvent('theme-changed', { detail: { theme: this.dark ? 'dark' : 'light' } }));
    },
    async saveSystemSettings() {
      this.settings.saving = true;
      try {
        const d = await this.api('/api/v0/settings', {
          method: 'PATCH',
          body: JSON.stringify({ default_theme: this.settings.default_theme })
        });
        if (d) {
          this.toast('System settings saved', 'success');
          // Apply the new theme immediately system-wide
          this.dark = this.settings.default_theme === 'dark';
          this.applyTheme();
        }
      } catch (e) {
        this.toast(e.message, 'error');
      } finally {
        this.settings.saving = false;
      }
    },
    async loadSystemSettings() {
      try {
        const resSettings = await fetch('/api/v0/settings');
        if (resSettings.ok) {
          const s = await resSettings.json();
          this.settings.default_theme = s.default_theme || 'dark';
        }
      } catch (e) {}
    },
    _dv() {
      if (this.hasAgentPerm('device_reset')) return 'device-reset';
      if (this.hasAgentPerm('unpick')) return 'unpick';
      if (this.isAdmin()) return 'users';
      return 'my-history';
    },
    isAdmin()      { return this.user && ['admin','superadmin'].includes(this.user.role); },
    isSuperadmin() { return this.user && this.user.role === 'superadmin'; },
    canSeeDeviceReset() { return this.hasAgentPerm('device_reset'); },
    canSeeUnpick()      { return this.hasAgentPerm('unpick'); },
    hasAgentPerm(p) { return !!this.user && (this.isAdmin() || (this.user.agent_perms||[]).includes(p)); },
    pageTitle() { return {dashboard:'Dashboard',['device-reset']:'Device Reset Agent',unpick:'Unpick Agent',users:'User Management',['db-config']:'DB Configurations',['audit-logs']:'System Audit Logs',['my-history']:'My Audit History',['agent-workflows']:'Agent Workflows',['system-settings']:'System Settings'}[this.view]||'Tychons Wi-Agents'; },
    nav(v) { this.view=v; this.sidebarOpen=false; this._lvd(); },
    _lvd() {
      if (this.view==='dashboard') { this.drLoadScheduler(); this.upLoadScheduler(); }
      else if (this.view==='device-reset') this.drLoadScheduler();
      else if (this.view==='unpick') this.upLoadScheduler();
      else if (this.view==='users') this.auLoad();
      else if (this.view==='db-config') this.dbcLoad();
      else if (this.view==='audit-logs') this.auLoadAuditLogs();
      else if (this.view==='my-history') this.myHistoryLoad();
      else if (this.view==='agent-workflows') this.awLoad();
      else if (this.view==='system-settings') this.loadSystemSettings();
    },
    async api(path, opts={}) {
      const getCookie = (name) => {
        const value = `; ${document.cookie}`;
        const parts = value.split(`; ${name}=`);
        if (parts.length === 2) return parts.pop().split(';').shift();
      };
      const token = getCookie('csrf_token');
      const cfg={credentials:'same-origin',...opts};
      const headers={...(cfg.headers||{})};
      if (token) headers['X-CSRFToken']=token;
      if (cfg.body&&typeof cfg.body==='string') headers['Content-Type']='application/json';
      cfg.headers=headers;
      const r=await fetch(path,cfg);
      if (r.status===401 && path !== '/api/v0/auth/login'){this.user=null;return null;}
      const d=await r.json();
      if (!r.ok) throw new Error(d.error||d.message||'Request failed');
      return d;
    },
    toast(msg,type='info') { const id=Date.now()+Math.random(); this.toasts.push({id,msg,type}); setTimeout(()=>{this.toasts=this.toasts.filter(t=>t.id!==id);},5000); },
    confirmDialog(msg) { return new Promise(resolve => { this.confirm = { show: true, msg, resolve }; }); },
    confirmOk()     { if (this.confirm.resolve) this.confirm.resolve(true);  this.confirm = { show: false, msg: '', resolve: null }; },
    confirmCancel() { if (this.confirm.resolve) this.confirm.resolve(false); this.confirm = { show: false, msg: '', resolve: null }; },

    async doLogin() {
      this.loginForm.loading=true; this.loginForm.error='';
      try {
        const d=await this.api('/api/v0/auth/login',{method:'POST',body:JSON.stringify({username:this.loginForm.username,password:this.loginForm.password})});
        if(d){
          this.user=d.user;
          this.loginForm={username:'',password:'',error:'',loading:false};
          this.view=this._dv();
          await this.loadDbConfigs();
          this._lvd();
          if (this.user && this.user.force_change_password) {
            this.openProfile();
          }
        }
      } catch(e){
        this.loginForm.error=e.message;
        this.loginForm.loading=false;
      }
    },
    async logout() { try { await this.api('/api/v0/auth/logout',{method:'POST'}); } finally { this.user=null; } },
    async loadDbConfigs() {
      try {
        const d=await this.api(this.isAdmin()?'/api/v0/admin/db_configs':'/api/v0/db_configs');
        if(!d)return;
        this.dbConfigs=this.isAdmin()?Object.entries(d.db_configs).map(([id,c])=>({id,...c})):d.db_configs;
      } catch(e){this.dbConfigs=[];}
    },
    async drLoadScheduler(){this.dr.schedulerLoading=true;try{this.dr.schedulerInfo=await this.api('/api/v0/device_reset_agent/scheduler_status');}catch(e){this.dr.schedulerInfo=null;}finally{this.dr.schedulerLoading=false;}},
    async drToggle(){try{await this.api('/api/v0/device_reset_agent/scheduler_toggle',{method:'POST'});await this.drLoadScheduler();this.toast('Scheduler updated','success');}catch(e){this.toast(e.message,'error');}},
    async drSetInterval(){const h=parseFloat(this.dr.intervalInput);if(!h||h<.25||h>168){this.toast('Interval must be 0.25–168 hours','error');return;}try{await this.api('/api/v0/device_reset_agent/scheduler_interval',{method:'POST',body:JSON.stringify({hours:h})});this.dr.intervalInput='';await this.drLoadScheduler();this.toast(`Interval set to ${h}h`,'success');}catch(e){this.toast(e.message,'error');}},
    async drManualReset(){const typeLabel=this.dr.manualInputType==='device'?'Device':'Employee';if(!this.dr.manualDbId||!this.dr.manualDeviceId.trim()){this.toast(`Select DB and enter ${typeLabel} ID`,'error');return;}if(!await this.confirmDialog(`Reset ${this.dr.manualInputType==='device'?'device':'employee'} "${this.dr.manualDeviceId.trim()}"? This will clear its assignment and relocate any inventory.`))return;this.dr.manualLoading=true;this.dr.manualResult=null;try{const d=await this.api('/api/v0/device_reset_agent/manual_reset',{method:'POST',body:JSON.stringify({db_config_id:this.dr.manualDbId,device_id:this.dr.manualDeviceId.trim(),input_type:this.dr.manualInputType})});if(d.type==='warning'){this.dr.manualResult={ok:false,type:'warning',message:d.message};this.toast(d.message,'warning');}else{this.dr.manualResult={ok:true,type:'success',steps:d.steps||[]};this.toast(`${typeLabel} reset successful`,'success');}}catch(e){this.dr.manualResult={ok:false,type:'error',message:e.message};this.toast(e.message,'error');}finally{this.dr.manualLoading=false;}},
    async drLoadLogs(){this.dr.logsLoading=true;try{const d=await this.api('/api/v0/device_reset_logs');this.dr.logs=d?d.logs:[];}catch(e){this.dr.logs=[];}finally{this.dr.logsLoading=false;}},
    drDownloadLogs(fmt){window.location.href=`/api/v0/device_reset_logs/download?format=${fmt}`;},
    async drAutoScan(){
      if(!this.dr.scanDbId){this.toast('Select a database','error');return;}
      this.dr.scanLoading=true;
      this.dr.scanResults=[];
      this.dr.execResults=[];
      try{
        const d=await this.api('/api/v0/device_reset_agent/auto_scan',{method:'POST',body:JSON.stringify({db_config_id:this.dr.scanDbId})});
        this.dr.scanResults=(d?.records||[]).map(r=>({...r,_selected:false}));
        this.toast(this.dr.scanResults.length?`Found ${this.dr.scanResults.length} record(s)`:'No stuck devices found',this.dr.scanResults.length?'success':'info');
      }catch(e){
        this.toast(e.message,'error');
      }finally{
        this.dr.scanLoading=false;
      }
    },
    drToggleAll(v){this.dr.scanResults.forEach(r=>r._selected=v);},
    drSelectedCount(){return this.dr.scanResults.filter(r=>r._selected).length;},
    async drExecuteSelected(){
      const devices=this.dr.scanResults.filter(r=>r._selected).map(({_selected,...r})=>r);
      if(!devices.length){this.toast('Select at least one device','error');return;}
      if(!await this.confirmDialog(`Execute device reset on ${devices.length} device(s)? This will modify warehouse records.`))return;
      this.dr.execLoading=true;
      this.dr.execResults=[];
      try{
        const d=await this.api('/api/v0/device_reset_agent/execute',{method:'POST',body:JSON.stringify({db_config_id:this.dr.scanDbId,devices})});
        this.dr.execResults=d?.results||[];
        const ok=this.dr.execResults.filter(r=>r.status==='SUCCESS').length;
        this.toast(`${ok}/${devices.length} devices processed`,ok===devices.length?'success':'warning');
      }catch(e){
        this.toast(e.message,'error');
      }finally{
        this.dr.execLoading=false;
      }
    },
    async upLoadScheduler(){this.up.schedulerLoading=true;try{this.up.schedulerInfo=await this.api('/api/v0/unpick_agent/scheduler_status');}catch(e){this.up.schedulerInfo=null;}finally{this.up.schedulerLoading=false;}},
    async upToggle(){try{await this.api('/api/v0/unpick_agent/scheduler_toggle',{method:'POST'});await this.upLoadScheduler();this.toast('Scheduler updated','success');}catch(e){this.toast(e.message,'error');}},
    async upSetInterval(){const h=parseFloat(this.up.intervalInput);if(!h||h<.25||h>168){this.toast('Interval must be 0.25–168 hours','error');return;}try{await this.api('/api/v0/unpick_agent/scheduler_interval',{method:'POST',body:JSON.stringify({hours:h})});this.up.intervalInput='';await this.upLoadScheduler();this.toast(`Interval set to ${h}h`,'success');}catch(e){this.toast(e.message,'error');}},
    async upAutoScan(){if(!this.up.scanDbId){this.toast('Select a database','error');return;}this.up.scanLoading=true;this.up.scanResults=[];this.up.execResults=[];try{const d=await this.api('/api/v0/unpick_agent/auto_scan',{method:'POST',body:JSON.stringify({db_config_id:this.up.scanDbId})});this.up.scanResults=(d?.records||[]).map(r=>({...r,_selected:false}));this.toast(this.up.scanResults.length?`Found ${this.up.scanResults.length} record(s)`:'No dirty records found',this.up.scanResults.length?'success':'info');}catch(e){this.toast(e.message,'error');}finally{this.up.scanLoading=false;}},
    upToggleAll(v){this.up.scanResults.forEach(r=>r._selected=v);},
    upSelectedCount(){return this.up.scanResults.filter(r=>r._selected).length;},
    async upExecuteSelected(){const records=this.up.scanResults.filter(r=>r._selected).map(({_selected,...r})=>r);if(!records.length){this.toast('Select at least one record','error');return;}if(!await this.confirmDialog(`Execute unpick on ${records.length} record(s)? This will modify warehouse inventory records.`))return;this.up.execLoading=true;this.up.execResults=[];try{const d=await this.api('/api/v0/unpick_agent/execute',{method:'POST',body:JSON.stringify({db_config_id:this.up.scanDbId,records})});this.up.execResults=d?.results||[];const ok=this.up.execResults.filter(r=>r.status==='SUCCESS').length;this.toast(`${ok}/${records.length} records processed`,ok===records.length?'success':'warning');}catch(e){this.toast(e.message,'error');}finally{this.up.execLoading=false;}},
    async upManualUnpick(){const{manualDbId:db_config_id,manualWhId:wh_id,manualOrderNum:order_number,manualItemNum:item_number}=this.up;if(!db_config_id||!wh_id||!order_number||!item_number){this.toast('All fields are required','error');return;}this.up.manualLoading=true;this.up.manualResult=null;try{const d=await this.api('/api/v0/unpick_agent/manual_unpick',{method:'POST',body:JSON.stringify({db_config_id,wh_id,order_number,item_number})});this.up.manualResult={ok:d.type!=='error',type:d.type||'success',message:d.message};this.toast(d.type==='warning'?d.message:'Manual unpick successful',d.type||'success');}catch(e){this.up.manualResult={ok:false,message:e.message};this.toast(e.message,'error');}finally{this.up.manualLoading=false;}},
    async upPartialUnpick(){const{partialDbId:db_config_id,partialWhId:wh_id,partialOrderNum:order_number,partialItemNum:item_number,partialQty}=this.up;if(!db_config_id||!wh_id||!order_number||!item_number||!partialQty){this.toast('All fields are required','error');return;}this.up.partialLoading=true;this.up.partialResult=null;try{const d=await this.api('/api/v0/unpick_agent/partial_unpick',{method:'POST',body:JSON.stringify({db_config_id,wh_id,order_number,item_number,unpick_qty:parseInt(partialQty,10)})});this.up.partialResult={ok:d.type!=='error',type:d.type||'success',message:d.message};this.toast(d.type==='warning'?d.message:'Partial unpick successful',d.type||'success');}catch(e){this.up.partialResult={ok:false,message:e.message};this.toast(e.message,'error');}finally{this.up.partialLoading=false;}},
    async upLoadLogs(){this.up.logsLoading=true;try{const d=await this.api('/api/v0/unpick_agent/logs');this.up.logs=d?d.logs:[];}catch(e){this.up.logs=[];}finally{this.up.logsLoading=false;}},
    upDownloadLogs(fmt){window.location.href=`/api/v0/unpick_agent/logs/download?format=${fmt}`;},
    async auLoad(){this.au.loading=true;try{const d=await this.api('/api/v0/admin/users');this.au.list=d?.users||[];}catch(e){this.toast(e.message,'error');}finally{this.au.loading=false;}},
    auOpenCreate(){this.au.editId=null;this.au.form={username:'',display_name:'',password:'',role:'user',agent_perms:[],is_active:true};this.au.modal=true;},
    auOpenEdit(u){this.au.editId=u.id;this.au.form={username:u.username,display_name:u.display_name||u.username,password:'',role:u.role,agent_perms:[...u.agent_perms],is_active:u.is_active};this.au.modal=true;},
    auTogglePerm(p){const i=this.au.form.agent_perms.indexOf(p);if(i===-1)this.au.form.agent_perms.push(p);else this.au.form.agent_perms.splice(i,1);},
    async auSave(){this.au.saving=true;try{if(this.au.editId){const p={display_name:this.au.form.display_name,role:this.au.form.role,agent_perms:this.au.form.agent_perms,is_active:this.au.form.is_active};if(this.au.form.password)p.password=this.au.form.password;await this.api(`/api/v0/admin/users/${this.au.editId}`,{method:'PATCH',body:JSON.stringify(p)});this.toast('User updated','success');}else{await this.api('/api/v0/admin/users',{method:'POST',body:JSON.stringify(this.au.form)});this.toast('User created','success');}this.au.modal=false;await this.auLoad();}catch(e){this.toast(e.message,'error');}finally{this.au.saving=false;}},
    async auDeactivate(id){if(!await this.confirmDialog('Deactivate this user? They will lose access immediately.'))return;try{await this.api(`/api/v0/admin/users/${id}`,{method:'DELETE'});this.toast('User deactivated','success');await this.auLoad();}catch(e){this.toast(e.message,'error');}},
    async auActivate(id){try{await this.api(`/api/v0/admin/users/${id}`,{method:'PATCH',body:JSON.stringify({is_active:true})});this.toast('User activated','success');await this.auLoad();}catch(e){this.toast(e.message,'error');}},
    async auLoadAuditLogs() {
      this.au.auditLoading = true;
      try {
        const d = await this.api('/api/v0/admin/audit_logs');
        this.au.auditLogs = d ? d.audit_logs : [];
      } catch(e) {
        this.toast(e.message, 'error');
        this.au.auditLogs = [];
      } finally {
        this.au.auditLoading = false;
      }
    },
    auDownloadAuditLogs(fmt) {
      window.location.href = `/api/v0/admin/audit_logs/download?format=${fmt}`;
    },
    auShowAuditDetail(log) {
      this.au.activeAudit = log;
      this.au.auditDetailModal = true;
    },
    cleanAuditDetails(details) {
      if (!details) return {};
      const c = {...details};
      delete c.ip_address;
      delete c.user_agent;
      return c;
    },
    filteredAuditLogs() {
      const q = (this.au.auditSearch || '').toLowerCase().trim();
      if (!q) return this.au.auditLogs;
      return this.au.auditLogs.filter(log => {
        const username = (log.username || '').toLowerCase();
        const action = (log.action || '').toLowerCase();
        const target = (log.target || '').toLowerCase();
        const ip = (log.details.ip_address || '').toLowerCase();
        return username.includes(q) || action.includes(q) || target.includes(q) || ip.includes(q);
      });
    },
    async loadAgents() {
      try {
        const d = await this.api('/api/v0/agents');
        this.registeredAgents = d ? d.agents : [];
      } catch(e) {
        this.registeredAgents = [
          {id: 'device_reset', name: 'Device Reset', description: 'Stuck device engine relocation agent'},
          {id: 'unpick', name: 'Unpick Agent', description: 'Stuck pick detail transaction log unpick agent'}
        ];
      }
    },
    async myHistoryLoad() {
      this.myHistoryLoading = true;
      try {
        const d = await this.api('/api/v0/auth/my_history');
        this.myHistory = d ? d.audit_logs : [];
      } catch(e) {
        this.toast(e.message, 'error');
        this.myHistory = [];
      } finally {
        this.myHistoryLoading = false;
      }
    },
    filteredMyHistory() {
      const q = (this.myHistorySearch || '').toLowerCase().trim();
      if (!q) return this.myHistory;
      return this.myHistory.filter(log => {
        const action = (log.action || '').toLowerCase();
        const target = (log.target || '').toLowerCase();
        const ip = (log.details.ip_address || '').toLowerCase();
        return action.includes(q) || target.includes(q) || ip.includes(q);
      });
    },
    async awLoad() {
      this.aw.loading = true;
      try {
        await this.loadAgents();
        this.aw.list = this.registeredAgents;
      } catch(e) {
        this.toast(e.message, 'error');
      } finally {
        this.aw.loading = false;
      }
    },
    awOpenCreate() {
      this.aw.editId = null;
      this.aw.error = '';
      this.aw.form = { id:'', name:'', description:'', flow_yaml:'' };
      this.aw.modal = true;
    },
    awOpenEdit(agent) {
      this.aw.editId = agent.id;
      this.aw.error = '';
      this.aw.form = { id: agent.id, name: agent.name, description: agent.description || '', flow_yaml: agent.flow_yaml || '' };
      this.aw.modal = true;
    },
    async awSave() {
      this.aw.saving = true;
      this.aw.error = '';
      try {
        if (this.aw.editId) {
          await this.api(`/api/v0/admin/agents/${this.aw.editId}`, {
            method: 'PATCH',
            body: JSON.stringify({
              name: this.aw.form.name,
              description: this.aw.form.description,
              flow_yaml: this.aw.form.flow_yaml
            })
          });
          this.toast('Agent workflow updated', 'success');
        } else {
          await this.api('/api/v0/admin/agents', {
            method: 'POST',
            body: JSON.stringify(this.aw.form)
          });
          this.toast('Agent workflow registered', 'success');
        }
        this.aw.modal = false;
        await this.awLoad();
      } catch(e) {
        this.aw.error = e.message;
        this.toast(e.message, 'error');
      } finally {
        this.aw.saving = false;
      }
    },
    async awDelete(id) {
      if (!await this.confirmDialog('Delete this agent workflow? This cannot be undone.')) return;
      try {
        await this.api(`/api/v0/admin/agents/${id}`, { method: 'DELETE' });
        this.toast('Agent workflow deleted', 'success');
        await this.awLoad();
      } catch(e) {
        this.toast(e.message, 'error');
      }
    },
    async dbcLoad(){this.dbc.loading=true;try{const d=await this.api('/api/v0/admin/db_configs');this.dbc.list=d?Object.entries(d.db_configs).map(([id,c])=>({id,...c})):[];}catch(e){this.toast(e.message,'error');}finally{this.dbc.loading=false;}},
    dbcOpenCreate(){
      this.dbc.editId=null;
      this.dbc.modalTab='connection';
      this.dbc.testConnResult=null;
      this.dbc.formTestResult=null;
      this.dbc.formTestLoading=false;
      this.dbc.logFormTestResult=null;
      this.dbc.logFormTestLoading=false;
      this.dbc.form={
        name:'',
        db_type:'mssql',
        db:{server:'',port:'',database:'',username:'',password:'',driver:''},
        use_log_db:false,
        log_db:{server:'',port:'',database:'',username:'',password:'',driver:''},
        notify:{teams_webhook:'',slack_webhook:'',report_after_run:false,on_error:true,on_warning:false}
      };
      this.dbc.modal=true;
    },
    dbcOpenEdit(c){
      this.dbc.editId=c.id;
      this.dbc.modalTab='connection';
      this.dbc.testConnResult=null;
      this.dbc.formTestResult=null;
      this.dbc.formTestLoading=false;
      this.dbc.logFormTestResult=null;
      this.dbc.logFormTestLoading=false;
      this.dbc.form={
        name:c.id,
        db_type:c.db_type||'mssql',
        db:{...c.db,password:''},
        use_log_db:!!c.log_db,
        log_db:c.log_db ? {...c.log_db,password:''} : {server:'',port:'',database:'',username:'',password:'',driver:''},
        notify:{teams_webhook:'',slack_webhook:'',report_after_run:false,on_error:true,on_warning:false,...c.notify}
      };
      this.dbc.modal=true;
    },
    dbcDefaultPort(){return this.dbc.form.db_type==='oracle'?'1521':'1433';},
    async dbcSave(){
      if(!this.dbc.form.name.trim()){this.toast('Name is required','error');return;}
      this.dbc.saving=true;
      try{
        const p={
          name:this.dbc.form.name.trim(),
          db_type:this.dbc.form.db_type,
          db:{...this.dbc.form.db},
          notify:{...this.dbc.form.notify}
        };
        if(!p.db.password)p.db.password='***';
        if(this.dbc.form.use_log_db){
          p.log_db={...this.dbc.form.log_db};
          if(!p.log_db.password)p.log_db.password='***';
        }else{
          p.log_db=null;
        }
        if(this.dbc.editId){
          await this.api(`/api/v0/admin/db_configs/${encodeURIComponent(this.dbc.editId)}`,{method:'PATCH',body:JSON.stringify(p)});
          this.toast('Config updated','success');
        }else{
          await this.api('/api/v0/admin/db_configs',{method:'POST',body:JSON.stringify(p)});
          this.toast('Config created','success');
        }
        this.dbc.modal=false;
        await this.dbcLoad();
        await this.loadDbConfigs();
      }catch(e){
        this.toast(e.message,'error');
      }finally{
        this.dbc.saving=false;
      }
    },
    async dbcDelete(id){if(!await this.confirmDialog(`Delete database config "${id}"? This cannot be undone.`))return;try{await this.api(`/api/v0/admin/db_configs/${encodeURIComponent(id)}`,{method:'DELETE'});this.toast('Config deleted','success');await this.dbcLoad();await this.loadDbConfigs();}catch(e){this.toast(e.message,'error');}},
    async dbcTestConn(id){this.dbc.testConnLoading=true;this.dbc.testConnResult=null;try{const d=await this.api(`/api/v0/admin/db_configs/${encodeURIComponent(id)}/test_connection`,{method:'POST'});this.dbc.testConnResult={ok:true,msg:d?.message||'Connection successful'};this.toast('Connection successful','success');}catch(e){this.dbc.testConnResult={ok:false,msg:e.message};this.toast(e.message,'error');}finally{this.dbc.testConnLoading=false;}},
    async dbcTestFormConn(target='primary'){
      const isLog = target === 'log';
      if(isLog){
        this.dbc.logFormTestLoading=true;
        this.dbc.logFormTestResult=null;
      }else{
        this.dbc.formTestLoading=true;
        this.dbc.formTestResult=null;
      }
      try {
        const p={
          name:this.dbc.form.name.trim(),
          db_type:this.dbc.form.db_type,
          target:target,
          db:{...this.dbc.form.db},
        };
        if(isLog){
          p.log_db={...this.dbc.form.log_db};
          if(!p.log_db.password) p.log_db.password='***';
        }else{
          if(!p.db.password) p.db.password='***';
        }
        const d=await this.api('/api/v0/admin/db_configs/test_connection',{method:'POST',body:JSON.stringify(p)});
        const resVal={ok:true,msg:d?.message||'Connection successful'};
        if(isLog) this.dbc.logFormTestResult=resVal;
        else this.dbc.formTestResult=resVal;
        this.toast('Connection successful','success');
      } catch(e) {
        const resVal={ok:false,msg:e.message};
        if(isLog) this.dbc.logFormTestResult=resVal;
        else this.dbc.formTestResult=resVal;
        this.toast(e.message,'error');
      } finally {
        if(isLog) this.dbc.logFormTestLoading=false;
        else this.dbc.formTestLoading=false;
      }
    },
    async dbcTestNotify(id,channel){try{await this.api('/api/v0/notify/test',{method:'POST',body:JSON.stringify({db_config_id:id,channel})});this.toast(`${channel} test message sent`,'success');}catch(e){this.toast(e.message,'error');}},
    openProfile(){this.profile.form={display_name:this.user.display_name||'',current_password:'',new_password:''};this.profile.modal=true;},
    async saveProfile(){this.profile.saving=true;try{const p={};if(this.profile.form.display_name.trim())p.display_name=this.profile.form.display_name.trim();if(this.profile.form.new_password){p.current_password=this.profile.form.current_password;p.new_password=this.profile.form.new_password;}await this.api('/api/v0/auth/profile',{method:'PATCH',body:JSON.stringify(p)});if(p.display_name)this.user={...this.user,display_name:p.display_name};if(this.profile.form.new_password){this.user.force_change_password=false;}this.profile.modal=false;this.toast('Profile updated','success');}catch(e){this.toast(e.message,'error');}finally{this.profile.saving=false;}},
    filteredUsers() {
      const q = (this.au.userSearch || '').toLowerCase().trim();
      if (!q) return this.au.list;
      return this.au.list.filter(u =>
        (u.username||'').toLowerCase().includes(q) ||
        (u.display_name||'').toLowerCase().includes(q) ||
        (u.role||'').toLowerCase().includes(q)
      );
    },
    relTime(ts) {
      if (!ts) return '—';
      const diff = Math.round((Date.now() - new Date(ts).getTime()) / 1000);
      if (isNaN(diff)) return ts;
      if (diff < 0) { const abs = Math.abs(diff); if (abs < 60) return 'in a moment'; if (abs < 3600) return `in ${Math.floor(abs/60)}m`; if (abs < 86400) return `in ${Math.floor(abs/3600)}h`; return `in ${Math.floor(abs/86400)}d`; }
      if (diff < 60) return 'just now';
      if (diff < 3600) return `${Math.floor(diff/60)}m ago`;
      if (diff < 86400) return `${Math.floor(diff/3600)}h ago`;
      if (diff < 2592000) return `${Math.floor(diff/86400)}d ago`;
      return new Date(ts).toLocaleDateString();
    },
    pwStrength(pw) {
      if (!pw) return {score:0,label:'',barColor:'bg-slate-200',textColor:'text-slate-400'};
      let s = 0;
      if (pw.length >= 8) s++;
      if (pw.length >= 12) s++;
      if (/[A-Z]/.test(pw) && /[a-z]/.test(pw)) s++;
      if (/[0-9]/.test(pw) && /[^A-Za-z0-9]/.test(pw)) s++;
      const map = [{label:'Weak',barColor:'bg-red-400',textColor:'text-red-500'},{label:'Fair',barColor:'bg-amber-400',textColor:'text-amber-600'},{label:'Good',barColor:'bg-lime-500',textColor:'text-lime-600'},{label:'Strong',barColor:'bg-emerald-500',textColor:'text-emerald-600'}];
      return {score:Math.max(1,s), ...(map[Math.max(0,s-1)])};
    },
    filteredDrLogs() {
      let logs = this.dr.logs;
      if (this.dr.logLevel) logs = logs.filter(e => e.level === this.dr.logLevel);
      const q = (this.dr.logSearch || '').toLowerCase().trim();
      if (q) logs = logs.filter(e => (e.message||'').toLowerCase().includes(q) || (e.device_id||'').toLowerCase().includes(q) || (e.run_id||'').toLowerCase().includes(q));
      return logs;
    },
    filteredUpLogs() {
      let logs = this.up.logs;
      if (this.up.logLevel) logs = logs.filter(e => e.level === this.up.logLevel);
      const q = (this.up.logSearch || '').toLowerCase().trim();
      if (q) logs = logs.filter(e => (e.message||'').toLowerCase().includes(q) || (e.order_number||'').toLowerCase().includes(q) || (e.item_number||'').toLowerCase().includes(q) || (e.run_id||'').toLowerCase().includes(q));
      return logs;
    },
    levelBadge(l){return({INFO:'bg-blue-100 text-blue-700',WARNING:'bg-amber-100 text-amber-700',ERROR:'bg-red-100 text-red-700',SUCCESS:'bg-emerald-100 text-emerald-700'})[l]||'bg-slate-100 text-slate-600';},
    statusBadge(s){return({SUCCESS:'bg-emerald-100 text-emerald-700',WARNING:'bg-amber-100 text-amber-700',ERROR:'bg-red-100 text-red-700'})[s]||'bg-slate-100 text-slate-600';},
    roleBadge(r){return({superadmin:'bg-purple-100 text-purple-700',admin:'bg-blue-100 text-blue-700',user:'bg-slate-100 text-slate-600'})[r]||'bg-slate-100 text-slate-600';},
    userInitials(u){if(!u)return'?';const n=u.display_name||u.username||'';return n.split(' ').map(w=>w[0]).join('').substring(0,2).toUpperCase()||'?';},
    fmtDate(iso){if(!iso)return'—';try{return new Date(iso).toLocaleString();}catch(e){return iso;}},
  }
}
