"""Local audio inspection and muxing. Never synthesizes audio or calls an API."""
from functools import lru_cache
import json
from pathlib import Path
import subprocess

from providers_jimeng import MotionCancelledError
from runtime_paths import quiet_subprocess_kwargs
import video_superresolution as sr


@lru_cache(maxsize=512)
def _inspect(path, size, modified):
    try:
        process=subprocess.run(['ffprobe','-v','error','-protocol_whitelist','file,pipe',
            '-format_whitelist','mov,mp3,wav,aac,flac,ogg','-show_entries',
            'stream=codec_type:format=duration','-of','json',path],check=True,capture_output=True,
            timeout=30,**quiet_subprocess_kwargs())
        data=json.loads(process.stdout)
        return {'has_audio':any(s.get('codec_type')=='audio' for s in data.get('streams',[])),
                'duration':float(data.get('format',{}).get('duration') or 0)}
    except (OSError,subprocess.SubprocessError,ValueError,KeyError):
        return {'has_audio':None,'duration':0}


def inspect(path):
    path=Path(path)
    if not path.is_file():return {'has_audio':False,'duration':0}
    info=path.stat()
    return dict(_inspect(str(path),info.st_size,info.st_mtime_ns))


def _run(command, output, stop, *, cwd=None):
    log=Path(output).with_suffix('.audio.log')
    try:
        sr.run_process(command,stop=stop,log=log,timeout=180,cwd=cwd)
    except MotionCancelledError:
        raise MotionCancelledError('声音处理已暂停，原片和原有片段已保留。') from None
    except RuntimeError:
        raise RuntimeError('本地声音合成未完成，请检查音频格式和文件是否完整。原片和原有片段已保留。') from None
    finally:
        log.unlink(missing_ok=True)


def mux(video, audio, output, *, start, length, stop=None):
    """Replace the audio only. Video packets, including AI frames, stay unchanged."""
    if inspect(audio)['has_audio'] is not True:
        raise ValueError('所选声音文件中没有可用的音轨。')
    _run(['ffmpeg','-hide_banner','-loglevel','error','-y','-i',str(video),'-ss',str(start),
          '-protocol_whitelist','file,pipe','-format_whitelist','mov,mp3,wav,aac,flac,ogg','-i',str(audio),
          '-map','0:v:0','-map','1:a:0','-c:v','copy','-c:a','aac','-b:a','192k','-ar','48000','-ac','2',
          '-af',f'apad,atrim=duration={length},asetpts=PTS-STARTPTS','-t',str(length),
          '-movflags','+faststart',str(output)],output,stop)
    if inspect(output)['has_audio'] is not True:
        raise RuntimeError('合成后未检测到音轨，原有片段保持原样。')


def wav(clip, output, *, length, stop=None):
    """Exact-duration PCM prevents AAC priming gaps when joining mixed shots."""
    inputs=['-i',str(clip)] if inspect(clip)['has_audio'] else ['-f','lavfi','-i','anullsrc=r=48000:cl=stereo']
    _run(['ffmpeg','-hide_banner','-loglevel','error','-y',*inputs,'-map','0:a:0','-vn',
          '-af',f'apad,atrim=duration={length},asetpts=PTS-STARTPTS','-ar','48000','-ac','2',
          '-c:a','pcm_s16le','-t',str(length),str(output)],output,stop)


def strip(video, output, *, length, stop=None):
    _run(['ffmpeg','-hide_banner','-loglevel','error','-y','-i',str(video),'-map','0:v:0',
          '-an','-c:v','copy','-t',str(length),'-movflags','+faststart',str(output)],output,stop)
