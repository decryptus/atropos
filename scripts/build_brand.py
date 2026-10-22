#!/usr/bin/env python3
"""Rebuild PNG/ICO assets from the editable SVG masters. Python 3, CairoSVG, Pillow."""
from pathlib import Path
import json
import cairosvg
from PIL import Image, ImageDraw
ROOT=Path(__file__).resolve().parents[1]/'assets'/'brand'

def render(svg, out, width, height=None):
    cairosvg.svg2png(url=str(svg),write_to=str(out),output_width=width,output_height=height)

def main():
    for sub in ['png','favicon']:(ROOT/sub).mkdir(exist_ok=True)
    for name in ['logo','logo-horizontal','logo-for-light','symbol','wordmark','logo-black','symbol-black','logo-white','symbol-white','logo-on-black','symbol-on-black']:
        for size in ([256,512,1024,2048] if name in ['logo','symbol'] else [512,1024]):
            render(ROOT/'svg'/f'atropos-{name}.svg',ROOT/'png'/f'atropos-{name}-{size}.png',size)
    for size in [16,32,48,64,128,256]:
        render(ROOT/'svg'/'atropos-symbol.svg',ROOT/'favicon'/f'favicon-{size}.png',size,size)
    im=Image.open(ROOT/'favicon'/'favicon-256.png')
    im.save(ROOT/'favicon'/'favicon.ico',sizes=[(s,s) for s in [16,32,48,64,128,256]])
    for name,size in [('apple-touch-icon',180),('android-chrome-192x192',192),('android-chrome-512x512',512)]:
        render(ROOT/'svg'/'atropos-symbol-on-black.svg',ROOT/'favicon'/f'{name}.png',size,size)
    (ROOT/'favicon'/'favicon.svg').write_text((ROOT/'svg'/'atropos-symbol.svg').read_text())
    (ROOT/'favicon'/'site.webmanifest').write_text(json.dumps({'name':'Atropos','short_name':'Atropos','icons':[{'src':'android-chrome-192x192.png','sizes':'192x192','type':'image/png'},{'src':'android-chrome-512x512.png','sizes':'512x512','type':'image/png'}],'theme_color':'#000000','background_color':'#000000','display':'standalone'},indent=2)+'\n')
    # Contact sheet, not a logo background variant.
    sheet=Image.new('RGB',(1440,950),'#252525'); d=ImageDraw.Draw(sheet)
    for x,bg,label in [(0,'#ffffff','TRANSPARENT / WHITE'),(720,'#000000','TRANSPARENT / BLACK')]:
        d.rectangle((x,0,x+719,949),fill=bg);d.text((x+24,20),label,fill='#888888')
        im=Image.open(ROOT/'png'/'atropos-logo-512.png');sheet.paste(im,(x+104,65),im)
        sym=Image.open(ROOT/'png'/'atropos-symbol-256.png');sheet.paste(sym,(x+72,590),sym)
        for i,s in enumerate([16,32,48,64]):
            im=Image.open(ROOT/'favicon'/f'favicon-{s}.png');sheet.paste(im,(x+365+i*75,660),im);d.text((x+365+i*75,740),str(s),fill='#888888')
    sheet.save(ROOT/'preview.png')
if __name__=='__main__':main()
