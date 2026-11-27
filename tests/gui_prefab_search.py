"""Actual Shift+A opens categories; immediate typing searches all metadata."""
import gzip,json,os,shutil,sys,tempfile,traceback
from pathlib import Path
from contextlib import closing
import bpy
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
if '--' in sys.argv:sys.path.insert(0,sys.argv[sys.argv.index('--')+1])
temporary=tempfile.TemporaryDirectory(prefix='ruster-search-')
os.environ['RUSTER_USER_DIR']=temporary.name
import ruster
from ruster import app,scene,studio,thumbnails
from ruster.assets import catalog
from ruster.core import settings
from tests.fixtures import triangle_asset
editor=Path(temporary.name)/'RustEdit'
preview_path='assets/bundled/prefabs/modding/cubes/tiled/cube_tiled_dev_01.prefab'
png=editor/'PreviewImages'/(preview_path+'.png')
png.parent.mkdir(parents=True)
shutil.copyfile(ROOT/'ruster/branding/icon-white.png',png)
(editor/'stringpool.txt').touch()
settings.save({'rustedit_install':str(editor)})
ruster.register()
bpy.context.window_manager.keyconfigs.update()
requests=[]
app.request_assets=lambda ids:requests.append(tuple(ids)) or True
with closing(catalog.connect(scene.cache_dir())) as db:
    rows=[(42,'assets/bundled/prefabs/modding/cubes/tiled/cube_tiled_dev_01.prefab'),
          (43,'assets/bundled/prefabs/modding/cubes/tiled/cube_tiled_dev_07.prefab'),
          (44,'assets/prefabs/building/wall.external_high.stone.prefab'),
          (147092032,'assets/prefabs/tools/hose/ioslothandles/ioslothandlehose_pending.prefab'),
          (4153457039,'assets/prefabs/tools/wire/ioslothandles/ioslothandle.prefab'),
          (46,'assets/prefabs/gloweyes/gloweyes.item.prefab'),
          (47,'assets/prefabs/gloweyes/gloweyes.wearable.prefab'),
          (50,'assets/prefabs/target/sound/target_deploy.prefab'),
          (51,'assets/prefabs/props/catalog_only_marker.prefab')]
    rows += [(200+i,f'assets/prefabs/props/page_item_{i:02}.prefab') for i in range(16)]
    db.executemany('INSERT INTO names VALUES(?,?)',rows)
    db.execute('INSERT INTO assets SELECT path,?,?,uid FROM names WHERE uid!=51',('bundle','file'));db.commit()
    model=scene.cache_dir()/'prefabs/215.json.gz'
    model.parent.mkdir(exist_ok=True)
    model.write_bytes(gzip.compress(json.dumps(triangle_asset(215)).encode()))
assert bpy.ops.ruster.load_thumbnail(uid='215')=={'FINISHED'}
queries=[('dev',42,'add',''),('dev cube',42,'add',''),('DEV',42,'add',''),
         ('CuBe DeV',42,'add',''),('01 cube dev',42,'add',''),
         ('stone wall high external',44,'add',''),('io',147092032,'add',''),
         ('IO',147092032,'add',''),('io',4153457039,'second',''),
         ('glow',147092032,'add',''),('GLOW',147092032,'add',''),
         ('cyan glow',147092032,'add',''),('glow cyan',147092032,'add',''),
         # Like Blender's Add menu, A-Z starts search. Leading punctuation is
         # entered through the explicit Search All Prefabs action instead.
         ('(CYAN glow)',147092032,'search',''),
         ('glowing hose',147092032,'add',''),('fluid pipe',147092032,'add',''),
         ('neon',147092032,'add',''),('maze',147092032,'add',''),
         ('page item 15',215,'add',''),('EFFECT SOUND',50,'add',''),
         ('catalog only marker',51,'add',''),('catalog only marker',51,'search',''),
         ('dev cube',None,'empty','@PROPS'),
         ('dev cube',42,'add',''),('glow',None,'cancel',''),('zzzxnotfound',None,'empty','')]
state={'phase':0,'case':0}

def step():
    try:
        window=bpy.context.window_manager.windows[0]
        area=next(a for a in window.screen.areas if a.type=='VIEW_3D')
        region=next(r for r in area.regions if r.type=='WINDOW')
        query,uid,operation,category=queries[state['case']]
        with bpy.context.temp_override(window=window,area=area,region=region):
            phase=state['phase']
            x,y=region.x+region.width//2,region.y+region.height//2
            if phase==0:
                window.event_simulate(type='ESC',value='PRESS',x=x,y=y)
                scene.clear_scene()
                window.event_simulate(type='MOUSEMOVE',value='NOTHING',x=region.x+region.width//2,y=region.y+region.height//2)
            elif phase==1:
                state['requests']=len(requests)
                if category:bpy.ops.ruster.add_prefab('INVOKE_DEFAULT',category=category)
                elif operation=='search':bpy.ops.wm.search_single_menu('INVOKE_DEFAULT',menu_idname='RUSTER_MT_add')
                else:
                    # Key events carry cursor coordinates too. The default (0,0)
                    # would dispatch Shift+A to the Timeline rather than 3D View.
                    window.event_simulate(type='LEFT_SHIFT',value='PRESS',x=x,y=y)
                    window.event_simulate(type='A',value='PRESS',shift=True,x=x,y=y)
                    window.event_simulate(type='A',value='RELEASE',shift=True,x=x,y=y)
                    window.event_simulate(type='LEFT_SHIFT',value='RELEASE',x=x,y=y)
            elif phase==2:
                if state['case']==0:
                    assert thumbnails.icon_id(42,preview_path)>0,'Missing local PNG icon'
                    assert thumbnails.icon_id(43,rows[1][1])==0,'Missing PNG should use native fallback'
                if state['case']==0 and (output:=os.environ.get('RUSTER_SEARCH_SCREENSHOTS')):
                    path=Path(output);path.mkdir(parents=True,exist_ok=True)
                    bpy.ops.screen.screenshot(filepath=str(path/'native-prefab-categories.png'))
                digits=dict(zip('0123456789',('ZERO','ONE','TWO','THREE','FOUR','FIVE','SIX','SEVEN','EIGHT','NINE')))
                punctuation={'(':'NINE',')':'ZERO'}
                for char in query:
                    kind='SPACE' if char==' ' else punctuation.get(char,digits.get(char,char.upper()))
                    window.event_simulate(type=kind,value='PRESS',unicode=char,x=x,y=y)
                    window.event_simulate(type=kind,value='RELEASE',x=x,y=y)
            elif phase==3:
                assert not scene.prefab_roots(),'Typing placed a model'
                assert len(requests)==state['requests'],'Typing requested models'
                assert len(studio._menu_cache)<=studio.MENU_CACHE_LIMIT
                if operation=='second':
                    window.event_simulate(type='DOWN_ARROW',value='PRESS',x=x,y=y)
                    window.event_simulate(type='DOWN_ARROW',value='RELEASE',x=x,y=y)
                if output:=os.environ.get('RUSTER_SEARCH_SCREENSHOTS'):
                    if query=='glow' and operation=='add':
                        path=Path(output);path.mkdir(parents=True,exist_ok=True)
                        bpy.ops.screen.screenshot(filepath=str(path/'native-glow-search.png'))
                key='ESC' if operation=='cancel' else 'RET'
                window.event_simulate(type=key,value='PRESS',x=x,y=y);window.event_simulate(type=key,value='RELEASE',x=x,y=y)
            else:
                roots=scene.prefab_roots()
                assert (not roots if uid is None else len(roots)==1 and roots[0]['ruster_uid']==str(uid)),(query,[o.name for o in roots])
                print('NATIVE_SHIFT_A_QUERY_OK',query,uid,operation,flush=True)
                window.event_simulate(type='ESC',value='PRESS',x=x,y=y)
                if roots:bpy.data.objects.remove(roots[0],do_unlink=True)
                state['case']+=1
                if state['case']==len(queries):
                    assert thumbnails.icon_id(215,rows[-1][1])>0,'Missing generated thumbnail'
                    pixels=thumbnails._previews['215']
                    assert pixels.image_size[:]==(24,24) and any(pixels.icon_pixels[:])
                    ruster.unregister();temporary.cleanup()
                    print('RUSTER_GUI_NATIVE_PREFAB_SEARCH_OK',flush=True)
                    bpy.ops.wm.quit_blender();return None
                state['phase']=1;return .5
            state['phase']+=1
        return .6
    except Exception:
        traceback.print_exc();os._exit(1)

bpy.app.timers.register(step,first_interval=2)
