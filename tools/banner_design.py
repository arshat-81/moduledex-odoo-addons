#!/usr/bin/env python3
"""Render the ModuleDex editorial banner collection. Requires cairosvg and Pillow.
Run from an addon checkout to render that branch only; use --workspace PATH for all four.
"""
import argparse
import ast
import html
import io
from pathlib import Path
import re
from PIL import Image, ImageDraw
import cairosvg

# Copy is based on the features in each module's manifest and description.
PROFILES = {
 'data_insight_workbench': ('DATA / REPORTING', ['SQL Query', '& Reports'], 'Turn database questions', 'into usable reports.', ['Read-only SQL', 'Approval control', 'CSV export'], '#087F73', 'sql'),
 'mdx_external_id_finder': ('DEVELOPER TOOLS', ['XML ID', 'Finder'], 'Find the identifier.', 'Get straight to the record.', ['Find XML IDs', 'Inspect records', 'Create IDs'], '#2360C8', 'finder'),
 'mdx_security_simulator': ('SECURITY / SIMULATION', ['Access Rights', 'Simulator'], 'See the permissions', 'before you grant them.', ['User simulation', 'Record access', 'Zero changes'], '#067D69', 'simulator'),
 'mdx_access_manager': ('SECURITY / GOVERNANCE', ['Access Rights', 'Manager'], 'Understand every permission.', 'Trace every change.', ['Effective rights', 'Change preview', 'Audit trail'], '#6650B8', 'manager'),
 'mdx_access_control': ('SECURITY / ENFORCEMENT', ['User Access', 'Control'], 'Shape what users see.', 'Control what they can do.', ['Hide elements', 'Restrict records', 'Preview first'], '#A63E68', 'control'),
 'mdx_perf_auditor': ('PERFORMANCE / DIAGNOSTICS', ['Performance', 'Auditor'], 'Find what slows Odoo down.', 'Know what to fix next.', ['Ranked findings', 'Safe fixes', 'Health report'], '#A95818', 'performance'),
 'ai_module_migrator': ('AI / MODULE MIGRATION', ['Module', 'Upgrade AI'], 'Move custom addons', 'between Odoo versions.', ['AI migration', 'Upgrade / downgrade', 'Odoo 11–20'], '#594DC3', 'migration'),
 'mdx_backup_manager': ('OPERATIONS / RECOVERY', ['Verified', 'Database Backup'], 'A backup is only useful', 'when you can trust it.', ['Scheduled backups', 'Upload verification', 'Alerts'], '#117D67', 'backup'),
 'mdx_bigcommerce_connector': ('COMMERCE / INTEGRATION', ['BigCommerce', 'Connector'], 'Your store and your ERP.', 'Working together.', ['Two-way sync', 'Orders & stock', 'Multi-storefront'], '#414CC4', 'commerce'),
 'mdx_credential_vault': ('SECURITY / ENCRYPTION', ['Credential', 'Encryption'], 'Keep secrets encrypted.', 'Even inside your database.', ['Passwords', 'API keys', 'OAuth tokens'], '#167591', 'vault'),
 'mdx_mcp_server': ('AI / CONTROLLED AUTOMATION', ['MCP Server', '& Audit'], 'Connect AI to Odoo.', 'Keep every action accountable.', ['Tool permissions', 'Write approvals', 'Call logging'], '#5864C7', 'mcp'),
 'mdx_upgrade_scanner': ('DEVELOPER TOOLS / MIGRATION', ['Odoo 20', 'Upgrade Scanner'], 'Find deprecated code', 'before the upgrade finds it.', ['File & line', 'Replacement hints', 'Custom addons'], '#B04D34', 'scanner'),
}

class Canvas:
 def __init__(self, accent): self.parts=[]; self.accent=accent
 def rect(self,x,y,w,h,fill,rx=0,stroke=None):
  self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}"'+(f' stroke="{stroke}"' if stroke else '')+'/>')
 def text(self,x,y,value,size=24,fill='#F6F4EE',weight=500,mono=False):
  family='DejaVu Sans Mono' if mono else 'Montserrat, DejaVu Sans, sans-serif'
  self.parts.append(f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" font-weight="{weight}" fill="{fill}">{html.escape(value)}</text>')
 def path(self,d,stroke,sw=3,fill='none',extra=''):
  self.parts.append(f'<path d="{d}" stroke="{stroke}" stroke-width="{sw}" stroke-linecap="round" stroke-linejoin="round" fill="{fill}" {extra}/>')
 def circle(self,x,y,r,fill): self.parts.append(f'<circle cx="{x}" cy="{y}" r="{r}" fill="{fill}"/>')
 def panel(self,x,y,w,h,title):
  self.rect(x+7,y+10,w,h,'#101820',18);self.rect(x,y,w,h,'#202D37',18,'#40505C');self.text(x+24,y+39,title,22,weight=600);self.path(f'M{x+20} {y+59} H{x+w-20}','#40505C',1)
 def tag(self,x,y,value,active=True,w=150):
  self.rect(x,y,w,36,self.accent if active else '#354550',9);self.text(x+12,y+25,value,15,weight=600)
 def check(self,x,y):
  self.circle(x,y,14,self.accent);self.path(f'M{x-6} {y} l4 4 l8 -9','#FFFFFF',2)

def artwork(c,kind,phase):
 a=c.accent;muted='#A8BAC7';white='#F6F4EE'
 if kind in ('simulator','manager'):
  c.panel(923,212,572,331,'ACCESS SIMULATION' if kind=='simulator' else 'EFFECTIVE RIGHTS')
  c.text(948,309,'sale.order',22,muted,mono=True)
  for j,s in enumerate(['READ','WRITE','CREATE','DELETE']):c.text(1130+j*84,310,s,13,muted)
  for i,s in enumerate(['Model','Records','Fields']):
   c.text(949,366+i*57,s,20)
   for j in range(4):
    allowed=j<3 or i==2
    c.rect(1130+j*84,343+i*57,62,30,a if allowed else '#70504F',8)
    c.text(1150+j*84,364+i*57,'+' if allowed else '−',20)
  c.tag(977,564,'NO DATA CHANGED' if kind=='simulator' else 'TRACE TO GROUP',w=238)
  c.path('M1088 542 V563',a)
  if kind=='manager':c.text(1251,590,'Preview → Apply',18,muted)
 elif kind=='control':
  c.panel(919,211,578,360,'ROLE PROFILE / SALES')
  for i,(s,v) in enumerate([('Reporting menu','HIDDEN'),('Payment terms','READ ONLY'),('Delete action','BLOCKED'),('Record scope','OWN RECORDS')]):
   y=309+i*63;c.text(945,y,s,22);c.tag(1272,y-25,v,i==phase,w=196)
  c.text(956,616,'01  Configure   →   02  Preview   →   03  Enforce',17,muted)
 elif kind=='sql':
  c.panel(922,204,563,211,'READ-ONLY QUERY')
  for i,s in enumerate(['SELECT name, amount_total','FROM sale_order','WHERE state = \'sale\';']):c.text(948,306+i*34,s,22,'#8EDBC9',mono=True)
  c.path('M1205 416 V448',a);c.panel(1020,450,457,157,'REPORT PREVIEW')
  for i,w in enumerate([116,242,184]):c.rect(1050,531+i*20,w,9,a if i==phase%3 else '#708991',4)
  c.tag(923,565,'CSV EXPORT',w=170)
 elif kind=='finder':
  c.panel(923,210,568,128,'LOOKUP / XML ID');c.text(949,307,'base.main_company',26,'#9DBDFF',mono=True)
  c.path('M1208 338 V379 H1372',a,3,extra='stroke-dasharray="7 7"')
  c.panel(923,381,354,183,'LINKED RECORD');c.text(948,479,'res.company',25);c.text(948,520,'Open · Inspect · Create',17,muted)
  c.circle(1394,468,68,'#263E5A');c.circle(1394,468,42,'#172A3C');c.path('M1375 468 l14 14 l26 -30','#9DBDFF',5)
 elif kind=='performance':
  c.panel(925,207,564,368,'DIAGNOSTICS / PRIORITY')
  for i,(s,w) in enumerate([('Database',324),('ORM',252),('Cron jobs',173),('Configuration',104)]):
   y=309+i*65;c.text(950,y,s,20,muted);c.rect(950,y+14,482,11,'#364651',5);c.rect(950,y+14,w,a if i==phase else '#D6AB74',5)
  c.tag(973,598,'FIND → RANK → FIX',w=265)
 elif kind=='migration':
  c.panel(921,223,252,265,'SOURCE ADDON');c.panel(1250,302,253,265,'MIGRATED ADDON')
  for x,y in [(947,332),(1276,411)]:
   for i,w in enumerate([167,116,150,90]):c.rect(x,y+i*26,w,9,a if i==phase else '#6B8090',4)
  c.path('M1175 366 H1230 m-13 -12 l13 12 l-13 12','#ACA4FF',4)
  c.tag(940,518,'ODOO 11–20',w=212);c.text(1258,608,'Review generated changes',16,muted)
 elif kind=='backup':
  c.panel(924,208,569,333,'BACKUP / VERIFICATION')
  for i,s in enumerate(['Create database archive','Upload to destination','Verify uploaded backup']):
   y=318+i*76;c.check(962,y-7);c.text(993,y,s,22);c.text(993,y+28,['Scheduled execution','Encrypted credentials','Verification recorded'][i],16,muted)
  c.tag(1000,577,'ALERT IF A BACKUP STOPS',w=348)
 elif kind=='commerce':
  c.panel(926,245,242,259,'BIGCOMMERCE');c.panel(1253,245,242,259,'ODOO')
  for x in [952,1280]:
   for i in range(3):c.rect(x,330+i*45,184,30,'#34485F',7);c.text(x+11,351+i*45,['Products','Orders','Inventory'][i],16)
  c.path('M1178 348 H1240 m-12 -10 l12 10 l-12 10','#A8B2FF',3);c.path('M1240 401 H1178 m12 -10 l-12 10 l12 10','#A8B2FF',3)
  c.tag(1045,550,'TWO-WAY SYNC',w=248);c.text(1018,617,'Customers · Refunds · Storefronts',19,muted)
 elif kind=='vault':
  c.panel(923,211,566,146,'CREDENTIAL FIELD');c.text(949,311,'API key / OAuth token',26,muted)
  c.path('M1200 357 V420',a,3,extra='stroke-dasharray="7 7"')
  c.rect(1126,409,148,120,a,19);c.path('M1161 409 V387 a39 39 0 0 1 78 0 V409','#9ADCEC',7);c.circle(1200,459,11,white);c.path('M1200 466 V487',white,5)
  c.panel(922,553,568,102,'DATABASE / ENCRYPTED AT REST');c.text(947,635,'gAAAAA...encrypted ciphertext...',18,'#9ADCEC',mono=True)
 elif kind=='mcp':
  for i,s in enumerate(['Claude','ChatGPT','Cursor']):
   c.rect(921,245+i*86,170,56,'#304156',12);c.text(941,281+i*86,s,22);c.path(f'M1092 {273+i*86} H1135 V359 H1170',a,2)
  c.panel(1166,242,330,253,'ODOO / MCP');
  for i,s in enumerate(['Tool permissions','Approve writes','Log every call']):c.check(1197,340+i*53);c.text(1220,347+i*53,s,18)
  c.tag(1010,566,'CONTROLLED AUTOMATION',w=366)
 elif kind=='scanner':
  c.panel(922,212,568,206,'CUSTOM ADDON / SOURCE')
  for i,s in enumerate(['models/order.py : 42','Deprecated API found','Replacement available']):c.text(948,310+i*35,s,22,'#FFBAA4' if i==1 else muted,mono=i==0)
  c.path('M1200 419 V453',a)
  c.panel(996,457,494,149,'UPGRADE FINDING');c.text(1022,555,'File → Line → Replacement',23);c.tag(940,636,'PREPARE FOR ODOO 20',w=301)

def svg_for(module,version,phase=0):
 category,title,line1,line2,chips,accent,kind=PROFILES[module];c=Canvas(accent)
 c.rect(0,0,1600,800,'#F5F2EA');c.rect(852,0,748,800,'#16232D')
 # A structural grid, an oversized orbit and diagonal corner marks form the visual identity.
 for x in range(876,1600,48): c.path(f'M{x} 0 V800','#24333E',1)
 for y in range(20,800,48): c.path(f'M852 {y} H1600','#24333E',1)
 c.parts.append(f'<circle cx="1464" cy="118" r="204" fill="none" stroke="{accent}" stroke-width="2" opacity=".65"/>')
 c.parts.append(f'<circle cx="1464" cy="118" r="173" fill="none" stroke="{accent}" stroke-width="1" opacity=".4"/>')
 c.rect(62,55,46,46,accent,12);c.path('M74 84 V69 l11 10 l11 -10 V84','#FFFFFF',3);c.text(122,87,'ModuleDex',29,'#18252F',700)
 c.text(62,163,category,17,accent,700);c.rect(62,185,67,5,accent,2)
 for i,line in enumerate(title): c.text(57,290+i*88,line,72,'#18252F',700)
 c.text(62,436,line1,28,'#50616B');c.text(62,478,line2,28,'#50616B')
 # Feature list is deliberately horizontal-free so long feature names stay legible.
 for i,s in enumerate(chips):
  c.circle(70,541+i*37,5,accent);c.text(87,548+i*37,s,21,'#334852',600)
 c.path('M62 692 H790','#D2D5CB',1);c.text(62,733,f'ODOO {version}  /  MODULEDEX ADDONS',16,'#50616B',600)
 c.text(922,91,'THE MODULEDEX COLLECTION',16,'#B6C7D2',600);c.text(922,133,kind.upper()+' / WORKFLOW',22,'#F6F4EE',600)
 artwork(c,kind,phase)
 c.circle(*[(1509,176),(1475,185),(1438,188),(1401,184)][phase],6,accent)
 c.text(922,732,'WORKFLOW ILLUSTRATION',13,'#A8BAC7',500);c.rect(1444,701,88,46,accent,11);c.text(1459,731,version,22,'#FFFFFF',700)
 return '<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="800" viewBox="0 0 1600 800" role="img"><title>'+html.escape('ModuleDex '+ ' '.join(title)+' for Odoo '+version)+'</title>'+''.join(c.parts)+'</svg>'

def render(svg):
 return Image.open(io.BytesIO(cairosvg.svg2png(bytestring=svg.encode()))).convert('RGB')

def build(root):
 branch_modules=sorted(root.glob('*/__manifest__.py'));count=0
 for manifest in branch_modules:
  module=manifest.parent.name
  if module not in PROFILES: raise ValueError(f'Missing design profile: {module}')
  version=ast.literal_eval(manifest.read_text())['version'].split('.')[0]+'.0'
  desc=manifest.parent/'static/description';desc.mkdir(parents=True,exist_ok=True)
  svg=svg_for(module,version);base=render(svg)
  existing=list(desc.glob('banner*'))
  (desc/'banner.svg').write_text(svg);base.save(desc/'banner.png',optimize=True)
  frames=None
  if any(p.suffix=='.gif' or p.name.startswith('banner_frame') or p.name=='banner_src' for p in existing):
   frames=[render(svg_for(module,version,i)) for i in range(4)]
  for p in existing:
   if p.suffix=='.gif':
    frames[0].save(p,save_all=True,append_images=frames[1:],duration=[1800]*4,loop=0,optimize=True,disposal=2)
   elif p.suffix=='.png' and p.name!='banner.png':
    match=re.search(r'frame_(\d+)',p.name);frame=frames[(int(match[1])-1)%4] if match else base;frame.save(p,optimize=True)
   elif p.is_dir() and p.name=='banner_src':
    for source in p.iterdir():
     match=re.search(r'frame_(\d+)',source.name);phase=(int(match[1])-1)%4 if match else 0
     if source.suffix=='.svg':source.write_text(svg_for(module,version,phase))
     elif source.suffix=='.png':frames[phase].save(source,optimize=True)
  page=desc/'index.html'
  if page.exists():
   content=page.read_text();content=re.sub(r'(src=["\']banner\.(?:png|gif))\?[^"\']+',r'\1',content)
   if not re.search(r'src=["\']banner\.',content):
    hero=f'\n  <div style="margin:0 0 34px; border-radius:16px; overflow:hidden;">\n    <img src="banner.png" alt="{html.escape(ast.literal_eval(manifest.read_text())["name"],quote=True)} — ModuleDex for Odoo {version}" style="width:100%; height:auto; display:block;"/>\n  </div>\n'
    content=re.sub(r'(<section\b[^>]*>)',lambda m:m[1]+hero,content,count=1)
   page.write_text(content)
  count+=1
 print(f'{root}: redesigned {count} module banners')
 return count

def main():
 p=argparse.ArgumentParser();p.add_argument('--workspace',type=Path);args=p.parse_args()
 roots=[Path.cwd()] if not args.workspace else [args.workspace/'odoo17/moduledex-odoo-addons',args.workspace/'odoo18/moduledex-odoo-addons',args.workspace/'odoo19/odoo/moduledex-odoo-addons',args.workspace/'odoo20/moduledex-odoo-addons']
 for root in roots:build(root)

if __name__=='__main__':main()
