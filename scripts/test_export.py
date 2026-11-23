import json, os, sys, shutil, runpy, glob, tempfile, unittest.mock as mock
import psycopg2
from openpyxl import Workbook, load_workbook

SRC = '/tmp/dqxen'
rows = []
for p in sorted(glob.glob(SRC + '/*.json')):
    f = os.path.basename(p)[:-5]
    d = json.load(open(p, encoding='utf-8'))
    for i, (eid, v) in enumerate(d.items()):
        ja, en = next(iter(v.items()))
        es = ('Poción ñandú ' + en) if i % 2 == 0 else ''     # mitad traducida, con tildes/ñ
        rows.append((f, eid, ja, en, es))
rows.append(('subPackage02Client', '__stale__9', '古い', 'old', 'viejo'))          # aparcada: no debe salir
rows += [('custom_mail', '10', 'メールA', 'Mail A', 'Correo ñ A'),
         ('custom_mail', '2', 'メールB', 'Mail B', 'Correo B'),
         ('custom_mail', '3', 'メールC', 'Mail C', ''),                            # sin ES: se omite
         ('custom_combat_chat', '1', 'せりふ', 'line', '')]                        # todo sin ES: no hay archivo

class Cur:
    def __init__(s, data): s.data = data; s.i = 0
    def __enter__(s): return s
    def __exit__(s, *a): pass
    def execute(s, sql, params=()):
        assert 'FROM entries' in sql
        s.rs = [r for r in rows if not r[1].startswith(params[1])]
        if len(params) > 2: s.rs = [r for r in s.rs if r[0] == params[2]]
        s.rs.sort(key=lambda r: (r[0], r[1]))
    def fetchmany(s, n):
        out, s.rs = s.rs[:n], s.rs[n:]; return out
class Conn:
    def cursor(s): return Cur(rows)
    def close(s): pass

work = tempfile.mkdtemp()
xlsx = os.path.join(work, 'merge.xlsx')
wb = Workbook(); wb.remove(wb.active)
from importlib import import_module
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import clarity_pack
for s, h in clarity_pack.MERGE_SHEETS.items(): wb.create_sheet(s).append(h)
wb['Dialogue'].append(['こんにちは', 'Aña', 'Hola, ¿qué tal? Ñandú', 'nota ñ', None])
wb.save(xlsx)
# clave real del glosario (fila par con ES de un archivo fuente): tiene que quedar fuera
IGN_KEY = next(r[2] for r in rows if r[0] == 'subPackage91Client' and r[4] and ',' not in r[2] and r[2] != r[4])
ign = os.path.join(work, 'ignore.py'); open(ign, 'w', encoding='utf-8').write('IGNORE = {\n    "%s": "x",\n    "otra": "y",\n}\n' % IGN_KEY)

def run(variant, extra):
    out = os.path.join(work, variant + '_out'); pack = os.path.join(work, variant)
    argv = ['export_translations.py', '--lang', 'es', '--all', '--output', out, '--clarity-pack-dir', pack,
            '--merge-xlsx', xlsx, '--ignore-file', ign] + extra
    with mock.patch.object(psycopg2, 'connect', lambda url: Conn()), mock.patch.dict(os.environ, {'DATABASE_URL': 'x'}), \
         mock.patch.object(sys, 'argv', argv):
        runpy.run_path(os.path.join(HERE, 'export_translations.py'), run_name='__main__')
    return out, pack

print('=== EXP'); oe, pe = run('exp', [])
print('=== VANILLA'); ov, pv = run('van', ['--clean-encoding'])

def chk(c, m): print(('OK   ' if c else 'FAIL ') + m); assert c, m
chk(not glob.glob(oe + '/custom_*'), 'custom_* NO esta en --output')
chk(not any('__stale__' in open(f, encoding='utf-8').read() for f in glob.glob(oe + '/*.json')), 'filas aparcadas excluidas')
cm = json.load(open(pe + '/dqxclarity/custom-translations/json/custom_mail.json', encoding='utf-8'))
chk(list(cm) == ['2', '10'] and 'メールC' not in str(cm), f'custom_mail: orden natural y sin entrada sin ES {list(cm)}')
chk(not os.path.exists(pe + '/dqxclarity/custom-translations/json/custom_combat_chat.json'), 'custom sin ES no genera archivo')
names = sorted(os.listdir(pe + '/dqxclarity/json'))
print('   6 nombres:', names)
chk('Correo ñ A' in str(cm) and 'Correo n' not in str(cm), 'exp conserva ñ')
cmv = json.load(open(pv + '/dqxclarity/custom-translations/json/custom_mail.json', encoding='utf-8'))
chk('Correo niA'.replace('niA','ni A') in str(cmv) and 'ñ' not in str(cmv), 'vanilla limpia ñ en custom')
gl = open(pv + '/dqxclarity/custom-translations/csv/glossary.csv', encoding='utf-8').read()
chk(gl and not any(c in gl for c in 'ñáéíóú'), 'glosario vanilla sin caracteres restringidos')
ge = open(pe + '/dqxclarity/custom-translations/csv/glossary.csv', encoding='utf-8').read()
chk('ñandú' in ge or 'Poción' in ge, 'glosario exp conserva tildes')
lens = [len(l.split(',', 1)[0].encode()) for l in ge.split('\n')]
chk(lens == sorted(lens, reverse=True), 'glosario ordenado por bytes de la clave desc')
keys = {l.split(',', 1)[0] for l in ge.split('\n')}
chk(IGN_KEY not in keys, 'IGNORE aplicado a una clave real (%s)' % IGN_KEY)
chk(len(keys) > 1000, 'el resto del glosario sigue (%d claves)' % len(keys))
chk(all(l.count(',') >= 1 and l.split(',',1)[0] for l in ge.split('\n')), 'todas las filas del CSV parsean')
wbv = load_workbook(pv + '/dqxclarity/custom-translations/csv/merge.xlsx'); r = [c.value for c in wbv['Dialogue'][2]]
print('   merge vanilla fila:', r)
chk(r[0] == 'こんにちは' and r[1] == 'Ania' and r[2] == 'Hola, que tal? NIandu' and 'ñ' not in str(r) and 'Ñ' not in str(r), 'merge vanilla: col A intacta, resto limpio')
wbe = load_workbook(pe + '/dqxclarity/custom-translations/csv/merge.xlsx'); r = [c.value for c in wbe['Dialogue'][2]]
chk(r[1] == 'Aña', 'merge exp: copia intacta')
chk(set(os.listdir(oe)) == set(os.listdir(ov)), 'mismos archivos ETP en ambas variantes')
print('TODO OK')
