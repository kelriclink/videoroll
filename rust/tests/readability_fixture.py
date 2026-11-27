"""Generate original Python expected display cues and quality, no DB/network."""
import json
import sys
from dataclasses import asdict
from videoroll.apps.subtitle_service.processing import Segment
from videoroll.apps.subtitle_service.subtitle_readability import derive_readability_profile,optimize_subtitle_readability
from videoroll.apps.subtitle_service.subtitle_quality import analyze_subtitle_quality

def cue(start,end,text,secondary_text=None):
    return Segment(start=start,end=end,text=text,secondary_text=secondary_text)
def encode(rows):
    return [{"start_ms":round(r.start*1000),"end_ms":round(r.end*1000),"text":r.text,"secondary_text":r.secondary_text} for r in rows]

cases=[
 ("short-gap","en",False,[cue(0,.45,"short readable caption"),cue(2,3,"next")]),
 ("no-overlap","en",False,[cue(0,.45,"very long caption that needs more time"),cue(.55,1.5,"next")]),
 ("cjk","zh",False,[cue(0,4,"这是一个用于测试字幕可读性优化器的比较长的中文字幕句子，这是下一句话。")]),
 ("punctuation","en",False,[cue(0,5,"This is the first clause, and this is the second clause, followed by a final phrase. " * 2)]),
 ("bilingual","zh",True,[cue(1,4,"这是一段相当长的中文字幕，需要在显示的时候进行平衡断行","This is a fairly long source subtitle that should also be balanced for display.")]),
 ("too-short","en",False,[cue(0,.8,"This is an extremely long subtitle that would normally require several display events." * 3)]),
 ("japanese","ja",False,[cue(1,5,"これは字幕の読みやすさをテストする文章です。次の文章では別の話題について説明します。")]),
 ("unicode","en",False,[cue(0,4,"Café — déjà vu. naïve résumé; full width ＡＢＣ. " * 3)]),
 ("overlap","en",False,[cue(0,2,"First sentence."),cue(1.8,3,"Second sentence.")]),
 ("scene","zh",False,[cue(i*3,i*3+1,"测试字幕。") for i in range(8)]),
]
out=[]
for name,lang,bilingual,rows in cases:
    p=derive_readability_profile(rows,target_lang=lang)
    result=optimize_subtitle_readability(rows,bilingual=bilingual,**p)
    report=analyze_subtitle_quality(source_segments=rows,translated_segments=rows,final_segments=result.segments,target_lang=lang)
    out.append(dict(name=name,lang=lang,bilingual=bilingual,input=encode(rows),profile=p,output=encode(result.segments),stats=asdict(result.stats),report=report))
with open(sys.argv[1],"w") as f:json.dump(out,f,ensure_ascii=False,indent=2)
print("generated",len(out),"Python reference fixtures")
