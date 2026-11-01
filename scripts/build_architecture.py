#!/usr/bin/env python3
"""Build self-contained, bilingual architecture SVGs from existing vector logos."""
from pathlib import Path
import re
import xml.etree.ElementTree as ET
from html import escape

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'assets/diagrams'
NS = 'http://www.w3.org/2000/svg'
ET.register_namespace('', NS)


def logo(path, key, x, y, w, h):
    node = ET.fromstring(path.read_text())
    for element in node.iter():
        if 'id' in element.attrib:
            element.set('id', key + '-' + element.get('id'))
        for attr, value in list(element.attrib.items()):
            value = re.sub(r'url\(#([^)]*)\)', lambda m: f'url(#{key}-{m[1]})', value)
            if attr == 'aria-labelledby':
                value = ' '.join(key + '-' + item for item in value.split())
            element.set(attr, value)
    node.attrib.update(x=str(x), y=str(y), width=str(w), height=str(h))
    return ET.tostring(node, encoding='unicode')


def build(fr=False):
    def tr(en, french): return french if fr else en
    parts = [f'''<svg xmlns="{NS}" width="650" height="572" viewBox="0 0 650 572" role="img" aria-labelledby="title desc">
<title id="title">{tr('Atropos architecture', 'Architecture d’Atropos')}</title>
<desc id="desc">{tr('Atraxis calls the authenticated API. The engine, local terminal and persistent database belong to Atropos. Auton and Alertmanager are optional integrations.', 'Atraxis appelle l’API authentifiée. Le moteur, le terminal local et la base persistante appartiennent à Atropos. Auton et Alertmanager sont des intégrations facultatives.')}</desc>
<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M0 0 10 5 0 10Z" fill="#84929d"/></marker></defs>
<style>text{{font-family:DejaVu Sans,Arial,sans-serif;fill:#253846}}.title{{font-size:16px;font-weight:500}}.body{{font-size:13px}}.small{{font-size:11px;fill:#637480}}.tag{{font-size:10px;letter-spacing:1.3px;fill:#637480}}.line{{fill:none;stroke:#84929d;stroke-width:1;marker-end:url(#arrow)}}.rw{{marker-start:url(#arrow)}}.box{{fill:white;stroke:#dce3e8;stroke-width:1}}</style>
<rect width="650" height="572" fill="white"/>''']
    def text(x,y,value,cls='body',anchor='start'):
        parts.append(f'<text x="{x}" y="{y}" class="{cls}" text-anchor="{anchor}">{escape(value)}</text>')
    def box(x,y,w,h):
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="5" class="box"/>')
    def line(d,rw=False):
        parts.append(f'<path d="{d}" class="line{" rw" if rw else ""}"/>')
    text(36,26,tr('APPLICATIONS AND INTEGRATIONS','APPLICATIONS ET INTÉGRATIONS'),'tag')
    parts.append(logo(OUT/'logos/atraxis.svg','atraxis',38,45,32,32))
    text(84,58,'Atraxis','title')
    text(84,78,tr('Web and terminal interfaces','Interfaces web et terminal'),'small')
    text(84,96,tr('or another API application','ou une autre application API'),'small')
    line('M178 106 V150')
    text(192,121,'atropos-client','small')
    text(192,138,tr('Authenticated API','API authentifiée'),'small')
    parts.append('<rect x="20" y="150" width="610" height="274" rx="8" fill="#fcfdfe" stroke="#c5cfd6" stroke-width="1"/>')
    parts.append(logo(ROOT/'assets/brand/svg/atropos-symbol.svg','atropos',38,160,32,32))
    text(84,181,'Atropos','title')
    text(612,181,tr('INTERNAL COMPONENTS','COMPOSANTS INTERNES'),'tag','end')
    line('M178 150 V202')
    parts.append('<g transform="translate(0 -16)">')
    box(36,218,280,180)
    text(52,245,tr('Engine','Moteur'),'title')
    text(52,269,tr('Rules · tests · permissions','Règles · tests · autorisations'))
    text(52,291,tr('Explicit workflow admission','Admission explicite des workflows'))
    parts.append('<path d="M52 310 H300" stroke="#e5eaee" stroke-width="1"/>')
    text(52,334,'Workers','body')
    text(52,357,tr('Execute authorized steps','Exécutent les étapes autorisées'),'small')
    text(52,379,tr('Record progress and results','Enregistrent l’avancement et les résultats'),'small')
    parts.append('<g transform="translate(-30 0)">')
    box(394,218,250,68)
    text(410,244,tr('Terminal','Terminal'),'title')
    text(410,267,tr('Local commands · trusted operators','Commandes locales · accès opérateur'),'small')
    parts.append('<path d="M394 337 C394 313 644 313 644 337 V388 C644 412 394 412 394 388 Z" fill="white" stroke="#9caeb9" stroke-width="1"/><ellipse cx="519" cy="337" rx="125" ry="14" fill="#fafcfd" stroke="#9caeb9" stroke-width="1"/>')
    text(519,369,tr('Database','Base de données'),'title','middle')
    text(519,390,tr('Rules · runs · audit history','Règles · exécutions · historique'),'small','middle')
    line('M519 286 V321',True)
    text(530,308,tr('read / write','lit / écrit'),'small')
    parts.append('</g>')
    line('M316 367 H361',True)
    text(340,342,tr('read','lit'),'small','middle')
    text(340,356,tr('/ write','/ écrit'),'small','middle')
    text(489,427,tr('SQLite by default','SQLite par défaut'),'small','middle')
    line('M126 398 V503')
    line('M271 398 V458 H466 V503')
    parts.append('<rect x="55" y="471" width="145" height="20" fill="white"/><rect x="377" y="471" width="180" height="20" fill="white"/>')
    text(126,485,tr('Authorized jobs','Tâches autorisées'),'small','middle')
    text(466,485,tr('Configured notifications','Notifications configurées'),'small','middle')
    parts.append(logo(OUT/'logos/auton.svg','auton',44,507,30,30))
    text(88,519,'Auton','title')
    text(88,540,tr('Execute jobs · optional adapter','Tâches · adaptateur optionnel'),'small')
    parts.append(logo(OUT/'logos/alertmanager.svg','alertmanager',364,507,30,30))
    text(408,519,'Alertmanager','title')
    text(408,540,tr('Notifications · optional adapter','Notifications · adaptateur optionnel'),'small')
    text(325,577,tr('Saving, testing or activating a rule does not start a workflow.','Enregistrer, tester ou activer une règle ne lance pas de workflow.'),'small','middle')
    parts.append('</g></svg>')
    path=OUT/('atropos-architecture-fr.svg' if fr else 'atropos-architecture.svg')
    path.write_text(''.join(parts)+'\n')


if __name__ == '__main__':
    build()
    build(fr=True)
