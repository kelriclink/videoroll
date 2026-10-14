from __future__ import annotations
import json
from unittest.mock import patch
import pytest

from videoroll.apps.dubbing.pipeline import generate_dubbed_media, segment_cache_key
from videoroll.apps.dubbing.speech_units import build_speech_units, normalize_spoken_text
from videoroll.apps.dubbing.timing import SpokenSegment
from videoroll.apps.dubbing.quality import normalize_quality
from videoroll.apps.dubbing.control import DubbingInterrupted
from tests.test_dubbing_pipeline import FakeSynthesizer, _make_input


def _p(i, start, end, text, voice='reference'):
    return SpokenSegment(i, start, end, text, text, voice)


def test_real_s100_fragments_become_one_natural_utterance_without_repeat():
    original = [
        _p(1, .32, 5.84, 'S100 电脑超级酷，即使你不熟悉 S100 这个术语，'),
        _p(2, 5.84, 11.56, '你大概率在过去的某个时候，见过、\n遇到过，甚至用过 S100 电脑，'),
        _p(3, 11.56, 12.56, '见过、遇到过，甚至用过一台 S100 电脑。'),
        _p(4, 12.56, 19.64, '比如 Altair 8800、MSI 8080、Northstar Horizon，'),
        _p(5, 19.64, 20.76, '还有很多。'),
    ]
    groups=build_speech_units(original,enabled=True)
    assert len(groups)==2
    assert groups[0].source_indices==(1,2,3)
    assert groups[0].part.start==.32 and groups[0].part.end==12.56
    assert groups[0].duplicate_removed
    assert groups[0].part.speech_text.count('见过、') == 1
    assert '一台 S100 电脑。' in groups[0].part.speech_text
    assert groups[1].source_indices==(4,5)
    assert original[1].speech_text.count('见过、')==1
    assert original[2].speech_text.startswith('见过、')  # Display subtitles unchanged.


def test_keep_speaker_boundaries_and_long_silence():
    original=[_p(1,1,2,'第一位说话人，','A'),_p(2,2,3,'下一位发言。','B'),
              _p(3,3.5,4,'然后重新开始，','B'),_p(4,4,5,'继续说。','B')]
    groups=build_speech_units(original,enabled=True)
    assert [g.source_indices for g in groups]==[(1,),(2,),(3,4)]
    assert len(build_speech_units(original,enabled=False))==4


def test_preserve_numbers_when_original_subtitles_split_at_decimal_dot():
    assert normalize_spoken_text('间距为 0.\n125 英寸。')=='间距为 0.125 英寸。'
    assert normalize_spoken_text('这就是\n我们的  S100')=='这就是我们的 S100'


def test_no_merging_across_overlaps_or_oversized_window():
    original=[_p(1,0,8,'这是前半句，'),_p(2,7.9,8.5,'字幕时间戳重叠。'),
              _p(3,8.5,25,'下一句过长，')]
    assert [g.source_indices for g in build_speech_units(original,enabled=True)]==[(1,),(2,),(3,)]


def test_pipeline_preview_synthesizes_one_merged_unit_not_three_subtitles(tmp_path):
    video, data = _make_input(tmp_path)
    original=[
        {'start':.3,'end':.9,'text':'你好，'},
        {'start':.9,'end':1.5,'text':'这是一段'},
        {'start':1.5,'end':2.2,'text':'完整的测试语音。'},
    ]
    data.write_text(json.dumps(original,ensure_ascii=False),encoding='utf8')
    seen=[]
    q=normalize_quality(model='Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice',auto_group_speech=True)
    engine=FakeSynthesizer()
    with pytest.raises(DubbingInterrupted,match='preview_ready'):
        generate_dubbed_media(video,data,tmp_path/'work',engine,quality=q,
                              preview_only=True,on_speech_units=seen.extend)
    assert len(engine.calls)==1
    assert engine.calls[0][0]=='你好，这是一段完整的测试语音。'
    assert seen[0]['source_indices']==[1,2,3]
    assert len(seen)==1
    assert seen[0]['index']==1
    assert data.read_text(encoding='utf8')==json.dumps(original,ensure_ascii=False)
    full=generate_dubbed_media(video,data,tmp_path/'work',engine,quality=q)
    assert len(engine.calls)==1  # Existing merged-unit cache reused.
    assert full.segments_total==1
    assert full.video_path.exists()


def test_voice_preview_endpoint_and_script_editor_follow_group_cache(tmp_path):
    """One WAV for three original subtitle rows, with no stale audio after edit."""
    from videoroll.apps.orchestrator_api.routers.dubbing import (
        list_dubbing_segments, preview_dubbing_segment, edit_dubbing_segment,
        DubbingSpeechUpdate,
    )
    from videoroll.apps.dubbing.pipeline import spoken_segments_from_json
    from videoroll.db.models import DubbingJob
    from tests.test_dubbing_production_controls import _prepare_job_media
    from pathlib import Path
    import subprocess

    factory,store,task_id,job_id=_prepare_job_media(tmp_path)
    raw=[
        {'start':.4,'end':.95,'text':'这一句，'},
        {'start':.95,'end':1.7,'text':'需要完成'},
        {'start':1.7,'end':2.5,'text':'才完整。'},
    ]
    path=store.path_for('sub/task/segments.json')
    path.write_text(json.dumps(raw,ensure_ascii=False),encoding='utf8')
    voice='Vivian';model='Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice'
    quality=normalize_quality(model=model,auto_group_speech=True)
    unit=build_speech_units(spoken_segments_from_json(raw,voice=voice),enabled=True)[0].part
    key=segment_cache_key(unit,model,quality=quality)
    wav=tmp_path/'speech.wav'
    subprocess.run(['ffmpeg','-loglevel','error','-y','-f','lavfi','-i','sine=frequency=330:duration=0.5',
                    '-ar','24000','-ac','1',str(wav)],check=True)
    stored=f'dub/{task_id}/tts/{key}.wav';store.upload_file(wav,stored)
    with factory() as db:
        job=db.get(DubbingJob,job_id)
        job.request_json={**job.request_json,'model_path':model,'voice':voice,
                          'quality':quality,'cache_schema':2}
        job.output_json={'segments':{key:stored}}
        job.status='preview_ready'
        db.commit()
    with factory() as db:
        listing=list_dubbing_segments(task_id,job_id,offset=0,limit=40,db=db,store=store)
        assert listing['total']==3
        assert [x['has_audio'] for x in listing['items']]==[True,False,False]
        assert listing['items'][0]['speech_unit_indices']==[1,2,3]
        assert listing['items'][0]['generated_speech_text']=='这一句，需要完成才完整。'
        assert listing['items'][1]['speech_unit_start']==1
        assert Path(preview_dubbing_segment(task_id,job_id,2,db=db,store=store).path)==store.path_for(stored)
    with factory() as db:
        edit_dubbing_segment(task_id,job_id,2,
                             DubbingSpeechUpdate(speech_text='应该把句子真正讲完'),db=db,store=store)
        after=list_dubbing_segments(task_id,job_id,offset=0,limit=40,db=db,store=store)
        assert after['items'][0]['has_audio'] is False
        assert '应该把句子真正讲完' in after['items'][0]['generated_speech_text']
