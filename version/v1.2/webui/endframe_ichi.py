from diffusers_helper.hf_login import login

import os
import random
import time
import subprocess
# クロスプラットフォーム対応のための条件付きインポート
try:
    import winsound
    HAS_WINSOUND = True
except ImportError:
    HAS_WINSOUND = False
import json
import traceback
from datetime import datetime, timedelta

os.environ['HF_HOME'] = os.path.abspath(os.path.realpath(os.path.join(os.path.dirname(__file__), './hf_download')))

# 設定モジュールをインポート（ローカルモジュール）
import os.path
from video_mode_settings import (
    VIDEO_MODE_SETTINGS, get_video_modes, get_video_seconds, get_important_keyframes, 
    get_copy_targets, get_max_keyframes_count, get_total_sections, generate_keyframe_guide_html,
    handle_mode_length_change, process_keyframe_change, MODE_TYPE_NORMAL, MODE_TYPE_LOOP
)

# インデックス変換のユーティリティ関数追加
def ui_to_code_index(ui_index):
    """UI表示のキーフレーム番号(1始まり)をコード内インデックス(0始まり)に変換"""
    return ui_index - 1

def code_to_ui_index(code_index):
    """コード内インデックス(0始まり)をUI表示のキーフレーム番号(1始まり)に変換"""
    return code_index + 1

import gradio as gr
import torch
import einops
import safetensors.torch as sf
import numpy as np
import argparse
import math

from PIL import Image
from diffusers import AutoencoderKLHunyuanVideo
from transformers import LlamaModel, CLIPTextModel, LlamaTokenizerFast, CLIPTokenizer
from diffusers_helper.hunyuan import encode_prompt_conds, vae_decode, vae_encode, vae_decode_fake
from diffusers_helper.utils import save_bcthw_as_mp4, crop_or_pad_yield_mask, soft_append_bcthw, resize_and_center_crop, state_dict_weighted_merge, state_dict_offset_merge, generate_timestamp
from diffusers_helper.models.hunyuan_video_packed import HunyuanVideoTransformer3DModelPacked
from diffusers_helper.pipelines.k_diffusion_hunyuan import sample_hunyuan
from diffusers_helper.memory import cpu, gpu, get_cuda_free_memory_gb, move_model_to_device_with_memory_preservation, offload_model_from_device_for_memory_preservation, fake_diffusers_current_device, DynamicSwapInstaller, unload_complete_models, load_model_as_complete
from diffusers_helper.thread_utils import AsyncStream, async_run
from diffusers_helper.gradio.progress_bar import make_progress_bar_css, make_progress_bar_html
from transformers import SiglipImageProcessor, SiglipVisionModel
from diffusers_helper.clip_vision import hf_clip_vision_encode
from diffusers_helper.bucket_tools import find_nearest_bucket


parser = argparse.ArgumentParser()
parser.add_argument('--share', action='store_true')
parser.add_argument("--server", type=str, default='127.0.0.1')
parser.add_argument("--port", type=int, default=8001)
parser.add_argument("--inbrowser", action='store_true')
args = parser.parse_args()

print(args)

free_mem_gb = get_cuda_free_memory_gb(gpu)
high_vram = free_mem_gb > 60

print(f'Free VRAM {free_mem_gb} GB')
print(f'High-VRAM Mode: {high_vram}')


# 元のモデル読み込みコード
try:
    text_encoder = LlamaModel.from_pretrained("hunyuanvideo-community/HunyuanVideo", subfolder='text_encoder', torch_dtype=torch.float16).cpu()
    text_encoder_2 = CLIPTextModel.from_pretrained("hunyuanvideo-community/HunyuanVideo", subfolder='text_encoder_2', torch_dtype=torch.float16).cpu()
    tokenizer = LlamaTokenizerFast.from_pretrained("hunyuanvideo-community/HunyuanVideo", subfolder='tokenizer')
    tokenizer_2 = CLIPTokenizer.from_pretrained("hunyuanvideo-community/HunyuanVideo", subfolder='tokenizer_2')
    vae = AutoencoderKLHunyuanVideo.from_pretrained("hunyuanvideo-community/HunyuanVideo", subfolder='vae', torch_dtype=torch.float16).cpu()
except Exception as e:
    print(f"モデル読み込みエラー: {e}")
    print("プログラムを終了します...")
    import sys
    sys.exit(1)

# 他のモデルも同様に例外処理
try:
    feature_extractor = SiglipImageProcessor.from_pretrained("lllyasviel/flux_redux_bfl", subfolder='feature_extractor')
    image_encoder = SiglipVisionModel.from_pretrained("lllyasviel/flux_redux_bfl", subfolder='image_encoder', torch_dtype=torch.float16).cpu()
    transformer = HunyuanVideoTransformer3DModelPacked.from_pretrained('lllyasviel/FramePackI2V_HY', torch_dtype=torch.bfloat16).cpu()
except Exception as e:
    print(f"モデル読み込みエラー (追加モデル): {e}")
    print("プログラムを終了します...")
    import sys
    sys.exit(1)

vae.eval()
text_encoder.eval()
text_encoder_2.eval()
image_encoder.eval()
transformer.eval()

if not high_vram:
    vae.enable_slicing()
    vae.enable_tiling()

transformer.high_quality_fp32_output_for_inference = True
print('transformer.high_quality_fp32_output_for_inference = True')

transformer.to(dtype=torch.bfloat16)
vae.to(dtype=torch.float16)
image_encoder.to(dtype=torch.float16)
text_encoder.to(dtype=torch.float16)
text_encoder_2.to(dtype=torch.float16)

vae.requires_grad_(False)
text_encoder.requires_grad_(False)
text_encoder_2.requires_grad_(False)
image_encoder.requires_grad_(False)
transformer.requires_grad_(False)

if not high_vram:
    # DynamicSwapInstaller is same as huggingface's enable_sequential_offload but 3x faster
    DynamicSwapInstaller.install_model(transformer, device=gpu)
    DynamicSwapInstaller.install_model(text_encoder, device=gpu)
else:
    text_encoder.to(gpu)
    text_encoder_2.to(gpu)
    image_encoder.to(gpu)
    vae.to(gpu)
    transformer.to(gpu)

stream = AsyncStream()

# 設定ファイル関連処理のリファクタリング
def get_settings_file_path():
    """設定ファイルの絶対パスを取得する"""
    base_path = os.path.dirname(os.path.abspath(__file__))
    settings_folder = os.path.join(base_path, 'settings')
    return os.path.join(settings_folder, 'app_settings.json')

def get_output_folder_path(folder_name=None):
    """出力フォルダの絶対パスを取得する"""
    base_path = os.path.dirname(os.path.abspath(__file__))
    if not folder_name or not folder_name.strip():
        folder_name = "outputs"
    return os.path.join(base_path, folder_name)

def initialize_settings():
    """設定ファイルを初期化する（存在しない場合のみ）"""
    settings_file = get_settings_file_path()
    settings_dir = os.path.dirname(settings_file)
    
    if not os.path.exists(settings_file):
        # 初期デフォルト設定
        default_settings = {'output_folder': 'outputs'}
        try:
            os.makedirs(settings_dir, exist_ok=True)
            with open(settings_file, 'w', encoding='utf-8') as f:
                json.dump(default_settings, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            print(f"設定ファイル初期化エラー: {e}")
            return False
    return True

def load_settings():
    """設定を読み込む関数"""
    settings_file = get_settings_file_path()
    default_settings = {'output_folder': 'outputs'}
    
    if os.path.exists(settings_file):
        try:
            with open(settings_file, 'r', encoding='utf-8') as f:
                file_content = f.read()
                if not file_content.strip():
                    return default_settings
                settings = json.loads(file_content)
                
                # デフォルト値とマージ
                for key, value in default_settings.items():
                    if key not in settings:
                        settings[key] = value
                return settings
        except Exception as e:
            print(f"設定読み込みエラー: {e}")
    
    return default_settings

def save_settings(settings):
    """設定を保存する関数"""
    settings_file = get_settings_file_path()
    
    try:
        # 保存前にディレクトリが存在するか確認
        os.makedirs(os.path.dirname(settings_file), exist_ok=True)
        
        # JSON書き込み
        with open(settings_file, 'w', encoding='utf-8') as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"設定保存エラー: {e}")
        return False

def open_output_folder(folder_path):
    """指定されたフォルダをOSに依存せず開く"""
    if not os.path.exists(folder_path):
        os.makedirs(folder_path, exist_ok=True)
    
    try:
        if os.name == 'nt':  # Windows
            subprocess.Popen(['explorer', folder_path])
        elif os.name == 'posix':  # Linux/Mac
            try:
                subprocess.Popen(['xdg-open', folder_path])
            except:
                subprocess.Popen(['open', folder_path])
        print(f"フォルダを開きました: {folder_path}")
        return True
    except Exception as e:
        print(f"フォルダを開く際にエラーが発生しました: {e}")
        return False

# フォルダ構造を先に定義
webui_folder = os.path.dirname(os.path.abspath(__file__))
presets_folder = os.path.join(webui_folder, 'presets')
os.makedirs(presets_folder, exist_ok=True)

# 設定保存用フォルダの設定
settings_folder = os.path.join(webui_folder, 'settings')
os.makedirs(settings_folder, exist_ok=True)

# 設定ファイル初期化
initialize_settings()

# ベースパスを定義
base_path = os.path.dirname(os.path.abspath(__file__))

# 設定から出力フォルダを取得
app_settings = load_settings()
output_folder_name = app_settings.get('output_folder', 'outputs')
print(f"設定から出力フォルダを読み込み: {output_folder_name}")

# 出力フォルダのフルパスを生成
outputs_folder = get_output_folder_path(output_folder_name)
os.makedirs(outputs_folder, exist_ok=True)

# 統一的なキーフレーム処理関数群

# 1. 統一的なキーフレーム変更ハンドラ
def unified_keyframe_change_handler(keyframe_idx, img, mode, length, enable_copy=True):
    """すべてのキーフレーム処理を統一的に行う関数
    
    Args:
        keyframe_idx: UIのキーフレーム番号-1 (0始まりのインデックス)
        img: 変更されたキーフレーム画像
        mode: モード ("通常" or "ループ")
        length: 動画長 ("6秒", "8秒", "10(5x2)秒", "12(4x3)秒", "16(4x4)秒")
        enable_copy: コピー機能が有効かどうか

    Returns:
        更新リスト: 変更するキーフレーム画像の更新情報のリスト
    """
    if img is None or not enable_copy:
        # 画像が指定されていない、またはコピー機能が無効の場合は何もしない
        max_keyframes = get_max_keyframes_count()
        remaining = max(0, max_keyframes - keyframe_idx - 1)
        return [gr.update() for _ in range(remaining)]
    
    # video_mode_settings.pyから定義されたコピーターゲットを取得
    targets = get_copy_targets(mode, length, keyframe_idx)
    
    # 結果の更新リスト作成
    max_keyframes = get_max_keyframes_count()
    updates = []
    
    # このキーフレーム以降のインデックスに対してのみ処理
    for i in range(keyframe_idx + 1, max_keyframes):
        # コピーパターン定義では相対インデックスでなく絶対インデックスが使われているため、
        # iがtargets内にあるかをチェック
        if i in targets:
            # コピー先リストに含まれている場合は画像をコピー
            updates.append(gr.update(value=img))
        else:
            # 含まれていない場合は変更なし
            updates.append(gr.update())
    
    return updates

# 2. モード変更の統一ハンドラ
def unified_mode_length_change_handler(mode, length, section_number_inputs):
    """モードと動画長の変更を統一的に処理する関数
    
    Args:
        mode: モード ("通常" or "ループ")
        length: 動画長 ("6秒", "8秒", "10(5x2)秒", "12(4x3)秒", "16(4x4)秒")
        section_number_inputs: セクション番号入力欄のリスト
        
    Returns:
        更新リスト: 各UI要素の更新情報のリスト
    """
    # 基本要素のクリア（入力画像と終了フレーム）
    updates = [gr.update(value=None) for _ in range(2)]
    
    # すべてのキーフレーム画像をクリア
    section_image_count = get_max_keyframes_count()
    for _ in range(section_image_count):
        updates.append(gr.update(value=None, elem_classes=""))
    
    # セクション番号ラベルをリセット
    for i in range(len(section_number_inputs)):
        section_number_inputs[i].elem_classes = ""
    
    # 重要なキーフレームを強調表示
    important_kfs = get_important_keyframes(length)
    for idx in important_kfs:
        ui_idx = code_to_ui_index(idx)
        update_idx = ui_idx + 1  # 入力画像と終了フレームの2つを考慮
        if update_idx < len(updates):
            updates[update_idx] = gr.update(value=None, elem_classes="highlighted-keyframe")
            if idx < len(section_number_inputs):
                section_number_inputs[idx].elem_classes = "highlighted-label"
    
    # ループモードの場合はキーフレーム1も強調（まだ強調されていない場合）
    if mode == MODE_TYPE_LOOP and 0 not in important_kfs:
        updates[2] = gr.update(value=None, elem_classes="highlighted-keyframe")
        if 0 < len(section_number_inputs):
            section_number_inputs[0].elem_classes = "highlighted-label"
    
    # 動画長の設定
    video_length = get_video_seconds(length)
    
    # 最終的な動画長設定を追加
    updates.append(gr.update(value=video_length))
    
    return updates

# 3. 入力画像変更の統一ハンドラ
def unified_input_image_change_handler(img, mode, length, enable_copy=True):
    """入力画像変更時の処理を統一的に行う関数
    
    Args:
        img: 変更された入力画像
        mode: モード ("通常" or "ループ")
        length: 動画長 ("6秒", "8秒", "10(5x2)秒", "12(4x3)秒", "16(4x4)秒")
        enable_copy: コピー機能が有効かどうか
        
    Returns:
        更新リスト: 終了フレームとすべてのキーフレーム画像の更新情報のリスト
    """
    if img is None or not enable_copy:
        # 画像が指定されていない、またはコピー機能が無効の場合は何もしない
        section_count = get_max_keyframes_count()
        return [gr.update() for _ in range(section_count + 1)]  # +1 for end_frame
    
    # ループモードかどうかで処理を分岐
    if mode == MODE_TYPE_LOOP:
        # ループモード: FinalFrameに入力画像をコピー
        updates = [gr.update(value=img)]  # end_frame
        
        # キーフレーム画像は更新なし
        section_count = get_max_keyframes_count()
        updates.extend([gr.update() for _ in range(section_count)])
        
    else:
        # 通常モード: FinalFrameは更新なし
        updates = [gr.update()]  # end_frame
        
        # 動画長/モードに基づいてコピー先のキーフレームを取得
        # これが設定ファイルに基づく方法
        copy_targets = []
        
        # 特殊処理のモードでは設定によって異なるキーフレームにコピー
        if length == "10(5x2)秒":
            # 10(5x2)秒の場合は5～8にコピー (インデックス4-7)
            copy_targets = [4, 5, 6, 7]
        elif length == "12(4x3)秒":
            # 12(4x3)秒の場合は7～9にコピー (インデックス6-8)
            copy_targets = [6, 7, 8]
        elif length == "16(4x4)秒":
            # 16(4x4)秒の場合は10～12にコピー (インデックス9-11)
            copy_targets = [9, 10, 11]
        elif length == "20(4x5)秒":
            # 20(4x5)秒の場合は13～15にコピー (インデックス12-14)
            copy_targets = [12, 13, 14]
        else:
            # 通常の動画長の場合は最初のいくつかのキーフレームにコピー
            if length == "6秒":
                copy_targets = [0, 1, 2, 3]  # キーフレーム1-4
            elif length == "8秒":
                copy_targets = [0, 1, 2, 3, 4, 5]  # キーフレーム1-6
        
        # キーフレーム画像の更新リスト作成
        section_count = get_max_keyframes_count()
        for i in range(section_count):
            if i in copy_targets:
                updates.append(gr.update(value=img))
            else:
                updates.append(gr.update())
    
    return updates

# 4. デバッグ情報表示関数 - コメントアウト部分を関数として維持
def print_keyframe_debug_info():
    """キーフレーム設定の詳細情報を表示"""
    # print("\n[INFO] =========== キーフレーム設定デバッグ情報 ===========")
    # 
    # # 設定内容の確認表示
    # print("\n[INFO] 動画モード設定の確認:")
    # for mode_key in VIDEO_MODE_SETTINGS:
    #     mode_info = VIDEO_MODE_SETTINGS[mode_key]
    #     print(f"  - {mode_key}: {mode_info['display_seconds']}秒, {mode_info['frames']}フレーム")
    #     
    #     # 重要キーフレームの表示（UIインデックスに変換）
    #     important_kfs = mode_info['important_keyframes']
    #     important_kfs_ui = [code_to_ui_index(kf) for kf in important_kfs]
    #     print(f"    重要キーフレーム: {important_kfs_ui}")
    #     
    #     # コピーパターンの表示
    #     for mode_type in ["通常", "ループ"]:
    #         if mode_type in mode_info["copy_patterns"]:
    #             print(f"    {mode_type}モードのコピーパターン:")
    #             for src, targets in mode_info["copy_patterns"][mode_type].items():
    #                 src_ui = code_to_ui_index(int(src))
    #                 targets_ui = [code_to_ui_index(t) for t in targets]
    #                 print(f"      キーフレーム{src_ui} → {targets_ui}")
    # 
    # print("[INFO] =================================================\n")
    pass


@torch.no_grad()
def worker(input_image, end_frame, prompt, n_prompt, seed, total_second_length, latent_window_size, steps, cfg, gs, rs, gpu_memory_preservation, use_teacache, save_section_frames, keep_section_videos, output_dir=None, section_settings=None):
    # 出力フォルダの設定
    global outputs_folder
    global output_folder_name
    if output_dir and output_dir.strip():
        # 出力フォルダパスを取得
        outputs_folder = get_output_folder_path(output_dir)
        print(f"出力フォルダを設定: {outputs_folder}")
        
        # フォルダ名が現在の設定と異なる場合は設定ファイルを更新
        if output_dir != output_folder_name:
            settings = load_settings()
            settings['output_folder'] = output_dir
            if save_settings(settings):
                output_folder_name = output_dir
                print(f"出力フォルダ設定を保存しました: {output_dir}")
    else:
        # デフォルト設定を使用
        outputs_folder = get_output_folder_path(output_folder_name)
        print(f"デフォルト出力フォルダを使用: {outputs_folder}")
    
    # フォルダが存在しない場合は作成
    os.makedirs(outputs_folder, exist_ok=True)
    # 処理時間計測の開始
    process_start_time = time.time()
    
    # 既存の計算方法を保持しつつ、設定からセクション数も取得する
    total_latent_sections = (total_second_length * 30) / (latent_window_size * 4)
    total_latent_sections = int(max(round(total_latent_sections), 1))
    
    # 現在のモードを取得（UIから渡された情報から）
    # セクション数を全セクション数として保存
    total_sections = total_latent_sections

    job_id = generate_timestamp()

    # セクション处理の詳細ログを出力
    latent_paddings = reversed(range(total_latent_sections))
    if total_latent_sections > 4:
        latent_paddings = [3] + [2] * (total_latent_sections - 3) + [1, 0]
    
    # 全セクション数を事前に計算して保存（イテレータの消費を防ぐため）
    latent_paddings_list = list(latent_paddings)
    total_sections = len(latent_paddings_list)
    latent_paddings = latent_paddings_list  # リストに変換したものを使用
    
    print(f"\u25a0 セクション生成詳細:")
    print(f"  - 生成予定セクション: {latent_paddings}")
    print(f"  - 各セクションのフレーム数: 約{latent_window_size * 4 - 3}フレーム")
    print(f"  - 合計セクション数: {total_sections}")
    
    stream.output_queue.push(('progress', (None, '', make_progress_bar_html(0, 'Starting ...'))))

    try:
        # セクション設定の前処理
        def get_section_settings_map(section_settings):
            """
            section_settings: DataFrame形式のリスト [[番号, 画像, プロンプト], ...]
            → {セクション番号: (画像, プロンプト)} のdict
            """
            result = {}
            if section_settings is not None:
                for row in section_settings:
                    if row and row[0] is not None:
                        sec_num = int(row[0])
                        img = row[1]
                        prm = row[2] if len(row) > 2 else ""
                        result[sec_num] = (img, prm)
            return result

        section_map = get_section_settings_map(section_settings)
        section_numbers_sorted = sorted(section_map.keys()) if section_map else []

        def get_section_info(i_section):
            """
            i_section: int
            section_map: {セクション番号: (画像, プロンプト)}
            指定がなければ次のセクション、なければNone
            """
            if not section_map:
                return None, None, None
            # i_section以降で最初に見つかる設定
            for sec in range(i_section, max(section_numbers_sorted)+1):
                if sec in section_map:
                    img, prm = section_map[sec]
                    return sec, img, prm
            return None, None, None
        
        # セクション固有のプロンプト処理を行う関数
        def process_section_prompt(i_section, section_map, llama_vec, clip_l_pooler, llama_attention_mask):
            """セクションに固有のプロンプトがあればエンコードして返す
            なければメインプロンプトのエンコード結果を返す
            返り値: (llama_vec, clip_l_pooler, llama_attention_mask)
            """
            if not isinstance(llama_vec, torch.Tensor) or not isinstance(llama_attention_mask, torch.Tensor):
                print("[ERROR] メインプロンプトのエンコード結果またはマスクが不正です")
                return llama_vec, clip_l_pooler, llama_attention_mask

            # セクション固有のプロンプトがあるか確認
            section_info = None
            if section_map:
                valid_section_nums = [k for k in section_map.keys() if k >= i_section]
                if valid_section_nums:
                    section_num = min(valid_section_nums)
                    section_info = section_map[section_num]
            
            # セクション固有のプロンプトがあれば使用
            if section_info and len(section_info) > 1:
                _, section_prompt = section_info
                if section_prompt and section_prompt.strip():
                    print(f"[section_prompt] セクション{i_section}の専用プロンプトを処理: {section_prompt[:30]}...")
                    
                    try:
                        # プロンプト処理
                        section_llama_vec, section_clip_l_pooler = encode_prompt_conds(
                            section_prompt, text_encoder, text_encoder_2, tokenizer, tokenizer_2
                        )
                        
                        # マスクの作成
                        section_llama_vec, section_llama_attention_mask = crop_or_pad_yield_mask(
                            section_llama_vec, length=512
                        )
                        
                        # データ型を明示的にメインプロンプトと合わせる
                        section_llama_vec = section_llama_vec.to(
                            dtype=llama_vec.dtype, device=llama_vec.device
                        )
                        section_clip_l_pooler = section_clip_l_pooler.to(
                            dtype=clip_l_pooler.dtype, device=clip_l_pooler.device
                        )
                        section_llama_attention_mask = section_llama_attention_mask.to(
                            device=llama_attention_mask.device
                        )
                        
                        return section_llama_vec, section_clip_l_pooler, section_llama_attention_mask
                    except Exception as e:
                        print(f"[ERROR] セクションプロンプト処理エラー: {e}")
            
            # 共通プロンプトを使用
            print(f"[section_prompt] セクション{i_section}は共通プロンプトを使用します")
            return llama_vec, clip_l_pooler, llama_attention_mask

        # Clean GPU
        if not high_vram:
            unload_complete_models(
                text_encoder, text_encoder_2, image_encoder, vae, transformer
            )

        # Text encoding

        stream.output_queue.push(('progress', (None, '', make_progress_bar_html(0, 'Text encoding ...'))))

        if not high_vram:
            fake_diffusers_current_device(text_encoder, gpu)  # since we only encode one text - that is one model move and one encode, offload is same time consumption since it is also one load and one encode.
            load_model_as_complete(text_encoder_2, target_device=gpu)

        llama_vec, clip_l_pooler = encode_prompt_conds(prompt, text_encoder, text_encoder_2, tokenizer, tokenizer_2)

        if cfg == 1:
            llama_vec_n, clip_l_pooler_n = torch.zeros_like(llama_vec), torch.zeros_like(clip_l_pooler)
        else:
            llama_vec_n, clip_l_pooler_n = encode_prompt_conds(n_prompt, text_encoder, text_encoder_2, tokenizer, tokenizer_2)

        llama_vec, llama_attention_mask = crop_or_pad_yield_mask(llama_vec, length=512)
        llama_vec_n, llama_attention_mask_n = crop_or_pad_yield_mask(llama_vec_n, length=512)

        # Processing input image

        stream.output_queue.push(('progress', (None, '', make_progress_bar_html(0, 'Image processing ...'))))

        def preprocess_image(img):
            H, W, C = img.shape
            height, width = find_nearest_bucket(H, W, resolution=640)
            img_np = resize_and_center_crop(img, target_width=width, target_height=height)
            img_pt = torch.from_numpy(img_np).float() / 127.5 - 1
            img_pt = img_pt.permute(2, 0, 1)[None, :, None]
            return img_np, img_pt, height, width

        input_image_np, input_image_pt, height, width = preprocess_image(input_image)
        Image.fromarray(input_image_np).save(os.path.join(outputs_folder, f'{job_id}.png'))

        # VAE encoding

        stream.output_queue.push(('progress', (None, '', make_progress_bar_html(0, 'VAE encoding ...'))))

        if not high_vram:
            load_model_as_complete(vae, target_device=gpu)

        start_latent = vae_encode(input_image_pt, vae)
        # end_frameも同じタイミングでencode
        if end_frame is not None:
            end_frame_np, end_frame_pt, _, _ = preprocess_image(end_frame)
            end_frame_latent = vae_encode(end_frame_pt, vae)
        else:
            end_frame_latent = None
            
        # create section_latents here
        section_latents = None
        if section_map:
            section_latents = {}
            for sec_num, (img, prm) in section_map.items():
                if img is not None:
                    # 画像をVAE encode
                    img_np, img_pt, _, _ = preprocess_image(img)
                    section_latents[sec_num] = vae_encode(img_pt, vae)

        # CLIP Vision

        stream.output_queue.push(('progress', (None, '', make_progress_bar_html(0, 'CLIP Vision encoding ...'))))

        if not high_vram:
            load_model_as_complete(image_encoder, target_device=gpu)

        image_encoder_output = hf_clip_vision_encode(input_image_np, feature_extractor, image_encoder)
        image_encoder_last_hidden_state = image_encoder_output.last_hidden_state

        # Dtype

        llama_vec = llama_vec.to(transformer.dtype)
        llama_vec_n = llama_vec_n.to(transformer.dtype)
        clip_l_pooler = clip_l_pooler.to(transformer.dtype)
        clip_l_pooler_n = clip_l_pooler_n.to(transformer.dtype)
        image_encoder_last_hidden_state = image_encoder_last_hidden_state.to(transformer.dtype)

        # Sampling

        stream.output_queue.push(('progress', (None, '', make_progress_bar_html(0, 'Start sampling ...'))))

        rnd = torch.Generator("cpu").manual_seed(seed)
        num_frames = latent_window_size * 4 - 3

        history_latents = torch.zeros(size=(1, 16, 1 + 2 + 16, height // 8, width // 8), dtype=torch.float32).cpu()
        history_pixels = None
        total_generated_latent_frames = 0

        latent_paddings = reversed(range(total_latent_sections))

        if total_latent_sections > 4:
            # In theory the latent_paddings should follow the above sequence, but it seems that duplicating some
            # items looks better than expanding it when total_latent_sections > 4
            # One can try to remove below trick and just
            # use `latent_paddings = list(reversed(range(total_latent_sections)))` to compare
            latent_paddings = [3] + [2] * (total_latent_sections - 3) + [1, 0]

        for i_section, latent_padding in enumerate(latent_paddings):
            # 先に変数を定義
            is_first_section = i_section == 0
            is_last_section = latent_padding == 0
            use_end_latent = is_last_section and end_frame is not None
            latent_padding_size = latent_padding * latent_window_size
            
            # 定義後にログ出力
            print(f"\n\u25a0 セクション{i_section}の処理開始 (パディング値: {latent_padding})")
            print(f"  - 現在の生成フレーム数: {total_generated_latent_frames * 4 - 3}フレーム")
            print(f"  - 生成予定フレーム数: {num_frames}フレーム")
            print(f"  - 最初のセクション?: {is_first_section}")
            print(f"  - 最後のセクション?: {is_last_section}")
            # set current_latent here
            # セクションごとのlatentを使う場合
            if section_map and section_latents is not None and len(section_latents) > 0:
                # i_section以上で最小のsection_latentsキーを探す
                valid_keys = [k for k in section_latents.keys() if k >= i_section]
                if valid_keys:
                    use_key = min(valid_keys)
                    current_latent = section_latents[use_key]
                    print(f"[section_latent] section {i_section}: use section {use_key} latent (section_map keys: {list(section_latents.keys())})")
                    print(f"[section_latent] current_latent id: {id(current_latent)}, min: {current_latent.min().item():.4f}, max: {current_latent.max().item():.4f}, mean: {current_latent.mean().item():.4f}")
                else:
                    current_latent = start_latent
                    print(f"[section_latent] section {i_section}: use start_latent (no section_latent >= {i_section})")
                    print(f"[section_latent] current_latent id: {id(current_latent)}, min: {current_latent.min().item():.4f}, max: {current_latent.max().item():.4f}, mean: {current_latent.mean().item():.4f}")
            else:
                current_latent = start_latent
                print(f"[section_latent] section {i_section}: use start_latent (no section_latents)")
                print(f"[section_latent] current_latent id: {id(current_latent)}, min: {current_latent.min().item():.4f}, max: {current_latent.max().item():.4f}, mean: {current_latent.mean().item():.4f}")

            if is_first_section and end_frame_latent is not None:
                history_latents[:, :, 0:1, :, :] = end_frame_latent

            if stream.input_queue.top() == 'end':
                stream.output_queue.push(('end', None))
                return

            # セクション固有のプロンプトがあれば使用する
            current_llama_vec, current_clip_l_pooler, current_llama_attention_mask = process_section_prompt(i_section, section_map, llama_vec, clip_l_pooler, llama_attention_mask)
            
            print(f'latent_padding_size = {latent_padding_size}, is_last_section = {is_last_section}')

            indices = torch.arange(0, sum([1, latent_padding_size, latent_window_size, 1, 2, 16])).unsqueeze(0)
            clean_latent_indices_pre, blank_indices, latent_indices, clean_latent_indices_post, clean_latent_2x_indices, clean_latent_4x_indices = indices.split([1, latent_padding_size, latent_window_size, 1, 2, 16], dim=1)
            clean_latent_indices = torch.cat([clean_latent_indices_pre, clean_latent_indices_post], dim=1)

            clean_latents_pre = current_latent.to(history_latents)
            clean_latents_post, clean_latents_2x, clean_latents_4x = history_latents[:, :, :1 + 2 + 16, :, :].split([1, 2, 16], dim=2)
            clean_latents = torch.cat([clean_latents_pre, clean_latents_post], dim=2)

            if not high_vram:
                unload_complete_models()
                # GPUメモリ保存値を明示的に浮動小数点に変換
                preserved_memory = float(gpu_memory_preservation) if gpu_memory_preservation is not None else 6.0
                print(f'Setting transformer memory preservation to: {preserved_memory} GB')
                move_model_to_device_with_memory_preservation(transformer, target_device=gpu, preserved_memory_gb=preserved_memory)

            if use_teacache:
                transformer.initialize_teacache(enable_teacache=True, num_steps=steps)
            else:
                transformer.initialize_teacache(enable_teacache=False)

            def callback(d):
                preview = d['denoised']
                preview = vae_decode_fake(preview)

                preview = (preview * 255.0).detach().cpu().numpy().clip(0, 255).astype(np.uint8)
                preview = einops.rearrange(preview, 'b c t h w -> (b h) (t w) c')

                if stream.input_queue.top() == 'end':
                    stream.output_queue.push(('end', None))
                    raise KeyboardInterrupt('User ends the task.')

                current_step = d['i'] + 1
                percentage = int(100.0 * current_step / steps)
                hint = f'Sampling {current_step}/{steps}'
                # セクション情報を追加（現在のセクション/全セクション）
                section_info = f'セクション: {i_section+1}/{total_sections}, '
                desc = f'{section_info}Total generated frames: {int(max(0, total_generated_latent_frames * 4 - 3))}, Video length: {max(0, (total_generated_latent_frames * 4 - 3) / 30) :.2f} seconds (FPS-30). The video is being extended now ...'
                stream.output_queue.push(('progress', (preview, desc, make_progress_bar_html(percentage, hint))))
                return

            generated_latents = sample_hunyuan(
                transformer=transformer,
                sampler='unipc',
                width=width,
                height=height,
                frames=num_frames,
                real_guidance_scale=cfg,
                distilled_guidance_scale=gs,
                guidance_rescale=rs,
                # shift=3.0,
                num_inference_steps=steps,
                generator=rnd,
                prompt_embeds=current_llama_vec,  # セクションごとのプロンプトを使用
                prompt_embeds_mask=current_llama_attention_mask,  # セクションごとのマスクを使用
                prompt_poolers=current_clip_l_pooler,  # セクションごとのプロンプトを使用
                negative_prompt_embeds=llama_vec_n,
                negative_prompt_embeds_mask=llama_attention_mask_n,
                negative_prompt_poolers=clip_l_pooler_n,
                device=gpu,
                dtype=torch.bfloat16,
                image_embeddings=image_encoder_last_hidden_state,
                latent_indices=latent_indices,
                clean_latents=clean_latents,
                clean_latent_indices=clean_latent_indices,
                clean_latents_2x=clean_latents_2x,
                clean_latent_2x_indices=clean_latent_2x_indices,
                clean_latents_4x=clean_latents_4x,
                clean_latent_4x_indices=clean_latent_4x_indices,
                callback=callback,
            )

            if is_last_section:
                generated_latents = torch.cat([start_latent.to(generated_latents), generated_latents], dim=2)

            total_generated_latent_frames += int(generated_latents.shape[2])
            history_latents = torch.cat([generated_latents.to(history_latents), history_latents], dim=2)

            if not high_vram:
                # 減圧時に使用するGPUメモリ値も明示的に浮動小数点に設定
                preserved_memory_offload = 8.0  # こちらは固定値のまま
                print(f'Offloading transformer with memory preservation: {preserved_memory_offload} GB')
                offload_model_from_device_for_memory_preservation(transformer, target_device=gpu, preserved_memory_gb=preserved_memory_offload)
                load_model_as_complete(vae, target_device=gpu)

            real_history_latents = history_latents[:, :, :total_generated_latent_frames, :, :]

            if history_pixels is None:
                history_pixels = vae_decode(real_history_latents, vae).cpu()
            else:
                section_latent_frames = (latent_window_size * 2 + 1) if is_last_section else (latent_window_size * 2)
                overlapped_frames = latent_window_size * 4 - 3

                current_pixels = vae_decode(real_history_latents[:, :, :section_latent_frames], vae).cpu()
                history_pixels = soft_append_bcthw(current_pixels, history_pixels, overlapped_frames)

            # 各セクションの最終フレームを静止画として保存（セクション番号付き）
            if save_section_frames and history_pixels is not None:
                try:
                    if i_section == 0 or current_pixels is None:
                        # 最初のセクションは history_pixels の最後
                        last_frame = history_pixels[0, :, -1, :, :]
                    else:
                        # 2セクション目以降は current_pixels の最後
                        last_frame = current_pixels[0, :, -1, :, :]
                    last_frame = einops.rearrange(last_frame, 'c h w -> h w c')
                    last_frame = last_frame.cpu().numpy()
                    last_frame = np.clip((last_frame * 127.5 + 127.5), 0, 255).astype(np.uint8)
                    last_frame = resize_and_center_crop(last_frame, target_width=width, target_height=height)
                    if is_first_section and end_frame is None:
                        Image.fromarray(last_frame).save(os.path.join(outputs_folder, f'{job_id}_{i_section}_end.png'))
                    else:
                        Image.fromarray(last_frame).save(os.path.join(outputs_folder, f'{job_id}_{i_section}.png'))
                except Exception as e:
                    print(f"[WARN] セクション{ i_section }最終フレーム画像保存時にエラー: {e}")

            if not high_vram:
                unload_complete_models()

            output_filename = os.path.join(outputs_folder, f'{job_id}_{total_generated_latent_frames}.mp4')

            save_bcthw_as_mp4(history_pixels, output_filename, fps=30)

            print(f'Decoded. Current latent shape {real_history_latents.shape}; pixel shape {history_pixels.shape}')

            print(f"\u25a0 セクション{i_section}の処理完了")
            print(f"  - 現在の累計フレーム数: {int(max(0, total_generated_latent_frames * 4 - 3))}フレーム")
            print(f"  - レンダリング時間: {max(0, (total_generated_latent_frames * 4 - 3) / 30) :.2f}秒")
            print(f"  - 出力ファイル: {output_filename}")

            stream.output_queue.push(('file', output_filename))

            if is_last_section:
                # 処理終了時に通知
                if HAS_WINSOUND:
                    winsound.PlaySound("SystemExclamation", winsound.SND_ALIAS)
                else:
                    print("\n✓ 処理が完了しました！")  # Linuxでの代替通知
                
                # 全体の処理時間を計算
                process_end_time = time.time()
                total_process_time = process_end_time - process_start_time
                hours, remainder = divmod(total_process_time, 3600)
                minutes, seconds = divmod(remainder, 60)
                time_str = ""
                if hours > 0:
                    time_str = f"{int(hours)}時間 {int(minutes)}分 {seconds:.1f}秒"
                elif minutes > 0:
                    time_str = f"{int(minutes)}分 {seconds:.1f}秒"
                else:
                    time_str = f"{seconds:.1f}秒"
                print(f"\n全体の処理時間: {time_str}")
                completion_message = f"すべてのセクション({total_sections}/{total_sections})が完了しました。全体の処理時間: {time_str}"
                stream.output_queue.push(('progress', (None, completion_message, make_progress_bar_html(100, '処理完了'))))
                
                # 中間ファイルの削除処理
                if not keep_section_videos:
                    # 最終動画のフルパス
                    final_video_path = output_filename
                    final_video_name = os.path.basename(final_video_path)
                    # job_id部分を取得（タイムスタンプ部分）
                    job_id_part = job_id
                    
                    # ディレクトリ内のすべてのファイルを取得
                    files = os.listdir(outputs_folder)
                    deleted_count = 0
                    
                    for file in files:
                        # 同じjob_idを持つMP4ファイルかチェック
                        if file.startswith(job_id_part) and file.endswith('.mp4') and file != final_video_name:
                            file_path = os.path.join(outputs_folder, file)
                            try:
                                os.remove(file_path)
                                deleted_count += 1
                                print(f"[削除] 中間ファイル: {file}")
                            except Exception as e:
                                print(f"[エラー] ファイル削除時のエラー {file}: {e}")
                    
                    if deleted_count > 0:
                        print(f"[済] {deleted_count}個の中間ファイルを削除しました。最終ファイルは保存されています: {final_video_name}")
                        stream.output_queue.push(('progress', (None, f"{deleted_count}個の中間ファイルを削除しました。最終動画は保存されています。", make_progress_bar_html(100, '処理完了'))))
                
                break
    except:
        traceback.print_exc()

        if not high_vram:
            unload_complete_models(
                text_encoder, text_encoder_2, image_encoder, vae, transformer
            )

    stream.output_queue.push(('end', None))
    return


def process(input_image, end_frame, prompt, n_prompt, seed, total_second_length, latent_window_size, steps, cfg, gs, rs, gpu_memory_preservation, use_teacache, use_random_seed, save_section_frames, keep_section_videos, output_dir, section_settings):
    global stream
    assert input_image is not None, 'No input image!'
    
    # 動画生成の設定情報をログに出力
    total_latent_sections = int(max(round((total_second_length * 30) / (latent_window_size * 4)), 1))
    
    mode_name = "通常モード" if mode_radio.value == MODE_TYPE_NORMAL else "ループモード"
    
    print(f"\n==== 動画生成開始 =====")
    print(f"\u25c6 生成モード: {mode_name}")
    print(f"\u25c6 動画長: {total_second_length}秒")
    print(f"\u25c6 生成セクション数: {total_latent_sections}回")
    print(f"\u25c6 サンプリングステップ数: {steps}")
    print(f"\u25c6 TeaCache使用: {use_teacache}")
    
    # セクションごとのキーフレーム画像の使用状況をログに出力
    valid_sections = []
    if section_settings is not None:
        for i, sec_data in enumerate(section_settings):
            if sec_data and sec_data[1] is not None:  # 画像が設定されている場合
                valid_sections.append(sec_data[0])
    
    if valid_sections:
        print(f"\u25c6 使用するキーフレーム画像: セクション{', '.join(map(str, valid_sections))}")
    else:
        print(f"\u25c6 キーフレーム画像: デフォルト設定のみ使用")
    
    print(f"=============================\n")

    if use_random_seed:
        seed = random.randint(0, 2**32 - 1)
        # UIのseed欄もランダム値で更新
        yield None, None, '', '', gr.update(interactive=False), gr.update(interactive=True), gr.update(value=seed)
    else:
        yield None, None, '', '', gr.update(interactive=False), gr.update(interactive=True), gr.update()

    stream = AsyncStream()
    
    # GPUメモリの設定値をデバッグ出力し、正しい型に変換
    gpu_memory_value = float(gpu_memory_preservation) if gpu_memory_preservation is not None else 6.0
    print(f'Using GPU memory preservation setting: {gpu_memory_value} GB')
    
    # 出力フォルダが空の場合はデフォルト値を使用
    if not output_dir or not output_dir.strip():
        output_dir = "outputs"
    print(f'Output directory: {output_dir}')

    async_run(worker, input_image, end_frame, prompt, n_prompt, seed, total_second_length, latent_window_size, steps, cfg, gs, rs, gpu_memory_value, use_teacache, save_section_frames, keep_section_videos, output_dir, section_settings)

    output_filename = None

    while True:
        flag, data = stream.output_queue.next()

        if flag == 'file':
            output_filename = data
            yield output_filename, gr.update(), gr.update(), gr.update(), gr.update(interactive=False), gr.update(interactive=True), gr.update()

        if flag == 'progress':
            preview, desc, html = data
            yield gr.update(), gr.update(visible=True, value=preview), desc, html, gr.update(interactive=False), gr.update(interactive=True), gr.update()

        if flag == 'end':
            yield output_filename, gr.update(visible=False), gr.update(), '', gr.update(interactive=True), gr.update(interactive=False), gr.update()
            break


def end_process():
    stream.input_queue.push('end')


# プリセット管理関連の関数
def initialize_presets():
    """初期プリセットファイルがない場合に作成する関数"""
    preset_file = os.path.join(presets_folder, 'prompt_presets.json')
    
    # デフォルトのプロンプト
    default_prompts = [
        'A character doing some simple body movements.',
        'A character uses expressive hand gestures and body language.',
        'A character walks leisurely with relaxed movements.',
        'A character performs dynamic movements with energy and flowing motion.',
        'A character moves in unexpected ways, with surprising transitions poses.',
    ]
    
    # デフォルト起動時プロンプト
    default_startup_prompt = "A character doing some simple body movements."
    
    # 既存ファイルがあり、正常に読み込める場合は終了
    if os.path.exists(preset_file):
        try:
            with open(preset_file, 'r', encoding='utf-8') as f:
                presets_data = json.load(f)
                
            # 起動時デフォルトがあるか確認
            startup_default_exists = any(preset.get("is_startup_default", False) for preset in presets_data.get("presets", []))
            
            # なければ追加
            if not startup_default_exists:
                presets_data.setdefault("presets", []).append({
                    "name": "起動時デフォルト",
                    "prompt": default_startup_prompt,
                    "timestamp": datetime.now().isoformat(),
                    "is_default": True,
                    "is_startup_default": True
                })
                presets_data["default_startup_prompt"] = default_startup_prompt
                
                with open(preset_file, 'w', encoding='utf-8') as f:
                    json.dump(presets_data, f, ensure_ascii=False, indent=2)
            return
        except:
            # エラーが発生した場合は新規作成
            pass
    
    # 新規作成
    presets_data = {
        "presets": [],
        "default_startup_prompt": default_startup_prompt
    }
    
    # デフォルトのプリセットを追加
    for i, prompt_text in enumerate(default_prompts):
        presets_data["presets"].append({
            "name": f"デフォルト {i+1}: {prompt_text[:20]}...",
            "prompt": prompt_text,
            "timestamp": datetime.now().isoformat(),
            "is_default": True
        })
    
    # 起動時デフォルトプリセットを追加
    presets_data["presets"].append({
        "name": "起動時デフォルト",
        "prompt": default_startup_prompt,
        "timestamp": datetime.now().isoformat(),
        "is_default": True,
        "is_startup_default": True
    })
    
    # 保存
    try:
        with open(preset_file, 'w', encoding='utf-8') as f:
            json.dump(presets_data, f, ensure_ascii=False, indent=2)
    except:
        # 保存に失敗してもエラーは出さない（次回起動時に再試行される）
        pass

def load_presets():
    """プリセットを読み込む関数"""
    preset_file = os.path.join(presets_folder, 'prompt_presets.json')
    
    # 初期化関数を呼び出し（初回実行時のみ作成される）
    initialize_presets()
    
    max_retries = 3
    retry_count = 0
    
    while retry_count < max_retries:
        try:
            with open(preset_file, 'r', encoding='utf-8') as f:
                file_contents = f.read()
                if not file_contents.strip():
                    print(f"読み込み時に空ファイルが検出されました: {preset_file}")
                    # 空ファイルの場合は再初期化を試みる
                    initialize_presets()
                    retry_count += 1
                    continue
                    
                data = json.loads(file_contents)
                print(f"プリセットファイル読み込み成功: {len(data.get('presets', []))}件")
                return data
                
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            # JSONパースエラーの場合はファイルが破損している可能性がある
            print(f"プリセットファイルの形式が不正です: {e}")
            # ファイルをバックアップ
            backup_file = f"{preset_file}.bak.{int(time.time())}"
            try:
                import shutil
                shutil.copy2(preset_file, backup_file)
                print(f"破損したファイルをバックアップしました: {backup_file}")
            except Exception as backup_error:
                print(f"バックアップ作成エラー: {backup_error}")
            
            # 再初期化
            initialize_presets()
            retry_count += 1
            
        except Exception as e:
            print(f"プリセット読み込みエラー: {e}")
            # エラー発生
            retry_count += 1
    
    # 再試行しても失敗した場合は空のデータを返す
    print("再試行しても読み込みに失敗しました。空のデータを返します。")
    return {"presets": []}

def get_default_startup_prompt():
    """起動時に表示するデフォルトプロンプトを取得する関数"""
    print("起動時デフォルトプロンプト読み込み開始")
    presets_data = load_presets()
    
    # プリセットからデフォルト起動時プロンプトを探す
    for preset in presets_data["presets"]:
        if preset.get("is_startup_default", False):
            startup_prompt = preset["prompt"]
            print(f"起動時デフォルトプロンプトを読み込み: '{startup_prompt[:30]}...' (長さ: {len(startup_prompt)}文字)")
            
            # 重複しているかチェック
            # 例えば「A character」が複数回出てくる場合は重複している可能性がある
            if "A character" in startup_prompt and startup_prompt.count("A character") > 1:
                print("プロンプトに重複が見つかりました。最初のセンテンスのみを使用します。")
                # 最初のセンテンスのみを使用
                sentences = startup_prompt.split(".")
                if len(sentences) > 0:
                    clean_prompt = sentences[0].strip() + "."
                    print(f"正規化されたプロンプト: '{clean_prompt}'")
                    return clean_prompt
                
            return startup_prompt
    
    # 見つからない場合はデフォルト設定を使用
    if "default_startup_prompt" in presets_data:
        default_prompt = presets_data["default_startup_prompt"]
        print(f"デフォルト設定から読み込み: '{default_prompt[:30]}...' (長さ: {len(default_prompt)}文字)")
        
        # 同様に重複チェック
        if "A character" in default_prompt and default_prompt.count("A character") > 1:
            print("デフォルトプロンプトに重複が見つかりました。最初のセンテンスのみを使用します。")
            sentences = default_prompt.split(".")
            if len(sentences) > 0:
                clean_prompt = sentences[0].strip() + "."
                print(f"正規化されたデフォルトプロンプト: '{clean_prompt}'")
                return clean_prompt
                
        return default_prompt
    
    # フォールバックとしてプログラムのデフォルト値を返す
    fallback_prompt = "A character doing some simple body movements."
    print(f"プログラムのデフォルト値を使用: '{fallback_prompt}'")
    return fallback_prompt

def save_preset(name, prompt_text):
    """プリセットを保存する関数"""
    
    presets_data = load_presets()
    
    if not name:
        # 名前が空の場合は起動時デフォルトとして保存
        # 既存の起動時デフォルトを探す
        startup_default_exists = False
        for preset in presets_data["presets"]:
            if preset.get("is_startup_default", False):
                # 既存の起動時デフォルトを更新
                preset["prompt"] = prompt_text
                preset["timestamp"] = datetime.now().isoformat()
                startup_default_exists = True
                # 起動時デフォルトを更新
                break
        
        if not startup_default_exists:
            # 見つからない場合は新規作成
            presets_data["presets"].append({
                "name": "起動時デフォルト",
                "prompt": prompt_text,
                "timestamp": datetime.now().isoformat(),
                "is_default": True,
                "is_startup_default": True
            })
            print(f"起動時デフォルトを新規作成: {prompt_text[:50]}...")
        
        # デフォルト設定も更新
        presets_data["default_startup_prompt"] = prompt_text
        
        preset_file = os.path.join(presets_folder, 'prompt_presets.json')
        try:
            # JSON直接書き込み
            with open(preset_file, 'w', encoding='utf-8') as f:
                json.dump(presets_data, f, ensure_ascii=False, indent=2)
            
            # プロンプトの値を更新
            if 'prompt' in globals():
                prompt.value = prompt_text
                
            return "プリセット '起動時デフォルト' を保存しました"
        except Exception as e:
            print(f"プリセット保存エラー: {e}")
            traceback.print_exc()
            return f"保存エラー: {e}"
    
    # 通常のプリセット保存処理
    # 同名のプリセットがあれば上書き、なければ追加
    preset_exists = False
    for preset in presets_data["presets"]:
        if preset["name"] == name:
            preset["prompt"] = prompt_text
            preset["timestamp"] = datetime.now().isoformat()
            preset_exists = True
            # 既存のプリセットを更新
            break
    
    if not preset_exists:
        presets_data["presets"].append({
            "name": name,
            "prompt": prompt_text,
            "timestamp": datetime.now().isoformat(),
            "is_default": False
        })
        # 新規プリセットを作成
    
    preset_file = os.path.join(presets_folder, 'prompt_presets.json')
    
    try:
        # JSON直接書き込み
        with open(preset_file, 'w', encoding='utf-8') as f:
            json.dump(presets_data, f, ensure_ascii=False, indent=2)
        
        # ファイル保存成功
        return f"プリセット '{name}' を保存しました"
    except Exception as e:
        print(f"プリセット保存エラー: {e}")
        # エラー発生
        return f"保存エラー: {e}"

def delete_preset(preset_name):
    """プリセットを削除する関数"""
    if not preset_name:
        return "プリセットを選択してください"
    
    presets_data = load_presets()
    
    # 削除対象のプリセットを確認
    target_preset = None
    for preset in presets_data["presets"]:
        if preset["name"] == preset_name:
            target_preset = preset
            break
    
    if not target_preset:
        return f"プリセット '{preset_name}' が見つかりません"
    
    # デフォルトプリセットは削除できない
    if target_preset.get("is_default", False):
        return f"デフォルトプリセットは削除できません"
    
    # プリセットを削除
    presets_data["presets"] = [p for p in presets_data["presets"] if p["name"] != preset_name]
    
    preset_file = os.path.join(presets_folder, 'prompt_presets.json')
    
    try:
        with open(preset_file, 'w', encoding='utf-8') as f:
            json.dump(presets_data, f, ensure_ascii=False, indent=2)
        
        return f"プリセット '{preset_name}' を削除しました"
    except Exception as e:
        return f"削除エラー: {e}"


# 既存のQuick Prompts（初期化時にプリセットに変換されるので、互換性のために残す）
quick_prompts = [
    'A character doing some simple body movements.',
    'A character uses expressive hand gestures and body language.',
    'A character walks leisurely with relaxed movements.',
    'A character performs dynamic movements with energy and flowing motion.',
    'A character moves in unexpected ways, with surprising transitions poses.',
]
quick_prompts = [[x] for x in quick_prompts]


css = make_progress_bar_css() + """
.title-suffix {
    color: currentColor;
    opacity: 0.05;
}

.highlighted-keyframe {
    border: 4px solid #ff3860 !important; 
    box-shadow: 0 0 10px rgba(255, 56, 96, 0.5) !important;
    background-color: rgba(255, 56, 96, 0.05) !important;
}

/* セクション番号ラベルの強調表示 */
.highlighted-label label {
    color: #ff3860 !important;
    font-weight: bold !important;
}
"""
block = gr.Blocks(css=css).queue()
with block:
    gr.HTML('<h1>FramePack<span class="title-suffix">-eichi</span></h1>')

    # デバッグ情報の表示
    # print_keyframe_debug_info()
    
    # モード選択用のラジオボタンと動画長選択用のラジオボタンを横並びに配置
    with gr.Row():
        with gr.Column(scale=1):
            mode_radio = gr.Radio(choices=[MODE_TYPE_NORMAL, MODE_TYPE_LOOP], value=MODE_TYPE_NORMAL, label="生成モード", info="通常：一般的な生成 / ループ：ループ動画用")
        with gr.Column(scale=1):
            # 設定から動的に選択肢を生成
            length_radio = gr.Radio(choices=get_video_modes(), value="6秒", label="動画長", info="キーフレーム画像のコピー範囲と動画の長さを設定")
    
    with gr.Row():
        with gr.Column():
            input_image = gr.Image(sources='upload', type="numpy", label="Image", height=320)
            end_frame = gr.Image(sources='upload', type="numpy", label="Final Frame (Optional)", height=320)
            
            with gr.Row():
                start_button = gr.Button(value="Start Generation")
                end_button = gr.Button(value="End Generation", interactive=False)
                
            prompt = gr.Textbox(label="Prompt", value=get_default_startup_prompt(), lines=6)

            with gr.Row():
                gr.Markdown("※プリセット名を空にして「保存」すると起動時デフォルトになります")
            
            # 互換性のためにQuick Listも残しておくが、非表示にする
            with gr.Row(visible=False):
                example_quick_prompts = gr.Dataset(samples=quick_prompts, label='Quick List', samples_per_page=1000, components=[prompt])             
                example_quick_prompts.click(lambda x: x[0], inputs=[example_quick_prompts], outputs=prompt, show_progress=False, queue=False)

            with gr.Group():
                use_teacache = gr.Checkbox(label='Use TeaCache', value=True, info='Faster speed, but often makes hands and fingers slightly worse.')

                # Use Random Seedの初期値
                use_random_seed_default = True
                seed_default = random.randint(0, 2**32 - 1) if use_random_seed_default else 1

                use_random_seed = gr.Checkbox(label="Use Random Seed", value=use_random_seed_default)

                n_prompt = gr.Textbox(label="Negative Prompt", value="", visible=False)  # Not used
                seed = gr.Number(label="Seed", value=seed_default, precision=0)

                def set_random_seed(is_checked):
                    if is_checked:
                        return random.randint(0, 2**32 - 1)
                    else:
                        return gr.update()
                use_random_seed.change(fn=set_random_seed, inputs=use_random_seed, outputs=seed)

                total_second_length = gr.Slider(label="Total Video Length (Seconds)", minimum=1, maximum=120, value=6, step=1)
                latent_window_size = gr.Slider(label="Latent Window Size", minimum=1, maximum=33, value=9, step=1, visible=False)  # Should not change
                steps = gr.Slider(label="Steps", minimum=1, maximum=100, value=25, step=1, info='Changing this value is not recommended.')

                cfg = gr.Slider(label="CFG Scale", minimum=1.0, maximum=32.0, value=1.0, step=0.01, visible=False)  # Should not change
                gs = gr.Slider(label="Distilled CFG Scale", minimum=1.0, maximum=32.0, value=10.0, step=0.01, info='Changing this value is not recommended.')
                rs = gr.Slider(label="CFG Re-Scale", minimum=0.0, maximum=1.0, value=0.0, step=0.01, visible=False)  # Should not change

                gpu_memory_preservation = gr.Slider(label="GPU Memory to Preserve (GB) (smaller = more VRAM usage)", minimum=6, maximum=128, value=9, step=0.1, info="空けておくGPUメモリ量を指定。小さい値=より多くのVRAMを使用可能=高速、大きい値=より少ないVRAMを使用=安全")

                # セクションごとの動画保存チェックボックスを追加（デフォルトOFF）
                keep_section_videos = gr.Checkbox(label="完了時にセクションごとの動画を残す", value=False, info="チェックがない場合は最終動画のみ保存されます（デフォルトOFF）")

                # セクションごとの静止画保存チェックボックスを追加（デフォルトOFF）
                save_section_frames = gr.Checkbox(label="セクションごとの静止画を保存", value=False, info="各セクションの最終フレームを静止画として保存します（デフォルトOFF）")
                
                # キーフレームコピー機能のオンオフ切り替え
                enable_keyframe_copy = gr.Checkbox(label="キーフレーム自動コピー機能を有効にする", value=True, info="オフにするとキーフレーム間の自動コピーが行われなくなります")
                
                # 出力フォルダ設定
                gr.Markdown("※ 出力先は `webui` 配下に限定されます")
                with gr.Row(equal_height=True):
                    with gr.Column(scale=4):
                        # フォルダ名だけを入力欄に設定
                        output_dir = gr.Textbox(
                            label="出力フォルダ名", 
                            value=output_folder_name,  # 設定から読み込んだ値を使用
                            info="動画やキーフレーム画像の保存先フォルダ名",
                            placeholder="outputs"
                        )
                    with gr.Column(scale=1, min_width=100):
                        open_folder_btn = gr.Button(value="📂 保存および出力フォルダを開く", size="sm")
                
                # 実際の出力パスを表示
                with gr.Row(visible=False):
                    path_display = gr.Textbox(
                        label="出力フォルダの完全パス",
                        value=os.path.join(base_path, output_folder_name),
                        interactive=False
                    )
                
                # フォルダを開くボタンのイベント
                def handle_open_folder_btn(folder_name):
                    """フォルダ名を保存し、そのフォルダを開く"""
                    if not folder_name or not folder_name.strip():
                        folder_name = "outputs"
                    
                    # フォルダパスを取得
                    folder_path = get_output_folder_path(folder_name)
                    
                    # 設定を更新して保存
                    settings = load_settings()
                    old_folder_name = settings.get('output_folder')
                    
                    if old_folder_name != folder_name:
                        settings['output_folder'] = folder_name
                        save_result = save_settings(settings)
                        if save_result:
                            # グローバル変数も更新
                            global output_folder_name, outputs_folder
                            output_folder_name = folder_name
                            outputs_folder = folder_path
                        print(f"出力フォルダ設定を保存しました: {folder_name}")
                    
                    # フォルダを開く
                    open_output_folder(folder_path)
                    
                    # 出力ディレクトリ入力欄とパス表示を更新
                    return gr.update(value=folder_name), gr.update(value=folder_path)
                
                open_folder_btn.click(fn=handle_open_folder_btn, inputs=[output_dir], outputs=[output_dir, path_display])

                # セクション設定（DataFrameをやめて個別入力欄に変更）
                # 設定から最大キーフレーム数を取得
                max_keyframes = get_max_keyframes_count()
                
                # セクション設定の入力欄を動的に生成
                section_number_inputs = []
                section_image_inputs = []
                section_prompt_inputs = []  # プロンプト入力欄用のリスト
                with gr.Group():
                    gr.Markdown("### セクション設定. セクション番号は動画の終わりからカウント.（任意。指定しない場合は通常のImage/プロンプトを使用）")
                    for i in range(max_keyframes):
                        with gr.Row():
                            # 左側にセクション番号とプロンプトを配置
                            with gr.Column(scale=1):
                                section_number = gr.Number(label=f"セクション番号{i+1}", value=i, precision=0)
                                section_prompt = gr.Textbox(label=f"セクションプロンプト{i+1}", placeholder="セクション固有のプロンプト（空白の場合は共通プロンプトを使用）", lines=2)
                            
                            # 右側にキーフレーム画像のみ配置
                            with gr.Column(scale=2):
                                section_image = gr.Image(label=f"キーフレーム画像{i+1}", sources="upload", type="numpy", height=200)
                            section_number_inputs.append(section_number)
                            section_image_inputs.append(section_image)
                            section_prompt_inputs.append(section_prompt)
                
                # 重要なキーフレームの説明
                with gr.Row():
                    with gr.Column():
                        # 設定から動的にHTML生成
                        note_html = gr.HTML(generate_keyframe_guide_html())
                        
                # section_settingsは入力欄の値をまとめてリスト化
                def collect_section_settings(*args):
                    # args: [num1, img1, prompt1, num2, img2, prompt2, ...]
                    return [[args[i], args[i+1], args[i+2]] for i in range(0, len(args), 3)]
                
                section_settings = gr.State([[None, None, ""] for _ in range(max_keyframes)])
                section_inputs = []
                for i in range(max_keyframes):
                    section_inputs.extend([section_number_inputs[i], section_image_inputs[i], section_prompt_inputs[i]])
                
                # section_inputsをまとめてsection_settings Stateに格納
                def update_section_settings(*args):
                    return collect_section_settings(*args)
                
                # section_inputsが変化したらsection_settings Stateを更新
                for inp in section_inputs:
                    inp.change(fn=update_section_settings, inputs=section_inputs, outputs=section_settings)
                
                # モード変更時の処理
                mode_radio.change(
                    fn=lambda mode, length: unified_mode_length_change_handler(mode, length, section_number_inputs),
                    inputs=[mode_radio, length_radio],
                    outputs=[input_image, end_frame] + section_image_inputs + [total_second_length]
                )
                
                # 動画長変更時の処理
                length_radio.change(
                    fn=lambda mode, length: unified_mode_length_change_handler(mode, length, section_number_inputs),
                    inputs=[mode_radio, length_radio],
                    outputs=[input_image, end_frame] + section_image_inputs + [total_second_length]
                )
                
                # 入力画像変更時の処理
                input_image.change(
                    fn=unified_input_image_change_handler,
                    inputs=[input_image, mode_radio, length_radio, enable_keyframe_copy],
                    outputs=[end_frame] + section_image_inputs
                )
                
                # 各キーフレーム画像の変更イベントを個別に設定
                # 一度に複数のコンポーネントを更新する代わりに、個別の更新関数を使用
                def create_single_keyframe_handler(src_idx, target_idx):
                    def handle_single_keyframe(img, mode, length, enable_copy):
                        # コピー条件をチェック
                        if img is None or not enable_copy:
                            return gr.update()
                        
                        # コピー先のチェック
                        targets = get_copy_targets(mode, length, src_idx)
                        if target_idx in targets:
                            return gr.update(value=img)
                        return gr.update()
                    return handle_single_keyframe
                
                # 各キーフレームについて、影響を受ける可能性のある後続のキーフレームごとに個別のイベントを設定
                for i, src_image in enumerate(section_image_inputs):
                    for j in range(i+1, len(section_image_inputs)):
                        src_image.change(
                            fn=create_single_keyframe_handler(i, j),
                            inputs=[src_image, mode_radio, length_radio, enable_keyframe_copy],
                            outputs=[section_image_inputs[j]]
                        )

        with gr.Column():
            result_video = gr.Video(label="Finished Frames", autoplay=True, show_share_button=False, height=512, loop=True)
            progress_desc = gr.Markdown('', elem_classes='no-generating-animation')
            progress_bar = gr.HTML('', elem_classes='no-generating-animation')
            preview_image = gr.Image(label="Next Latents", height=200, visible=False)
            
            # プロンプト管理パネルの追加
            with gr.Group(visible=True) as prompt_management:
                gr.Markdown("### プロンプト管理")
                
                # 編集画面を常時表示する
                with gr.Group(visible=True):
                    # 起動時デフォルトの初期表示用に取得
                    default_prompt = ""
                    default_name = ""
                    for preset in load_presets()["presets"]:
                        if preset.get("is_startup_default", False):
                            default_prompt = preset["prompt"]
                            default_name = preset["name"]
                            break
                    
                    with gr.Row():
                        edit_name = gr.Textbox(label="プリセット名", placeholder="名前を入力...", value=default_name)
                    
                    edit_prompt = gr.Textbox(label="プロンプト", lines=5, value=default_prompt)
                    
                    with gr.Row():
                        # 起動時デフォルトをデフォルト選択に設定
                        default_preset = "起動時デフォルト"
                        # プリセットデータから全プリセット名を取得
                        presets_data = load_presets()
                        choices = [preset["name"] for preset in presets_data["presets"]]
                        default_presets = [name for name in choices if any(p["name"] == name and p.get("is_default", False) for p in presets_data["presets"])]
                        user_presets = [name for name in choices if name not in default_presets]
                        sorted_choices = [(name, name) for name in sorted(default_presets) + sorted(user_presets)]
                        preset_dropdown = gr.Dropdown(label="プリセット", choices=sorted_choices, value=default_preset, type="value")

                    with gr.Row():
                        save_btn = gr.Button(value="保存", variant="primary")
                        apply_preset_btn = gr.Button(value="反映", variant="primary")
                        clear_btn = gr.Button(value="クリア")
                        delete_preset_btn = gr.Button(value="削除")
                
                # メッセージ表示用
                result_message = gr.Markdown("")
    
    # 実行ボタンのイベント
    ips = [input_image, end_frame, prompt, n_prompt, seed, total_second_length, latent_window_size, steps, cfg, gs, rs, gpu_memory_preservation, use_teacache, use_random_seed, save_section_frames, keep_section_videos, output_dir, section_settings]
    start_button.click(fn=process, inputs=ips, outputs=[result_video, preview_image, progress_desc, progress_bar, start_button, end_button, seed])
    end_button.click(fn=end_process)
    
    # プリセット保存ボタンのイベント
    def save_button_click_handler(name, prompt_text):
        """保存ボタンクリック時のハンドラ関数"""
        
        # 重複チェックと正規化
        if "A character" in prompt_text and prompt_text.count("A character") > 1:
            sentences = prompt_text.split(".")
            if len(sentences) > 0:
                prompt_text = sentences[0].strip() + "."
                # 重複を検出したため正規化
        
        # プリセット保存
        result_msg = save_preset(name, prompt_text)
        
        # プリセットデータを取得してドロップダウンを更新
        presets_data = load_presets()
        choices = [preset["name"] for preset in presets_data["presets"]]
        default_presets = [n for n in choices if any(p["name"] == n and p.get("is_default", False) for p in presets_data["presets"])]
        user_presets = [n for n in choices if n not in default_presets]
        sorted_choices = [(n, n) for n in sorted(default_presets) + sorted(user_presets)]
        
        # メインプロンプトは更新しない（保存のみを行う）
        return result_msg, gr.update(choices=sorted_choices), gr.update()
    
    # 保存ボタンのクリックイベントを接続
    save_btn.click(
        fn=save_button_click_handler,
        inputs=[edit_name, edit_prompt],
        outputs=[result_message, preset_dropdown, prompt]
    )
    
    # クリアボタン処理
    def clear_fields():
        return gr.update(value=""), gr.update(value="")
    
    clear_btn.click(
        fn=clear_fields,
        inputs=[],
        outputs=[edit_name, edit_prompt]
    )
    
    # プリセット読込処理
    def load_preset_handler(preset_name):
        # プリセット選択時に編集欄のみを更新
        for preset in load_presets()["presets"]:
            if preset["name"] == preset_name:
                return gr.update(value=preset_name), gr.update(value=preset["prompt"])
        return gr.update(), gr.update()

    # プリセット選択時に編集欄に反映
    def load_preset_handler_wrapper(preset_name):
        # プリセット名がタプルの場合も処理する
        if isinstance(preset_name, tuple) and len(preset_name) == 2:
            preset_name = preset_name[1]  # 値部分を取得
        return load_preset_handler(preset_name)

    preset_dropdown.change(
        fn=load_preset_handler_wrapper,
        inputs=[preset_dropdown],
        outputs=[edit_name, edit_prompt]
    )
    
    # 反映ボタン処理 - 編集画面の内容をメインプロンプトに反映
    def apply_to_prompt(edit_text):
        """編集画面の内容をメインプロンプトに反映する関数"""
        # 編集画面のプロンプトをメインに適用
        return gr.update(value=edit_text)

    # プリセット削除処理
    def delete_preset_handler(preset_name):
        # プリセット名がタプルの場合も処理する
        if isinstance(preset_name, tuple) and len(preset_name) == 2:
            preset_name = preset_name[1]  # 値部分を取得
        
        result = delete_preset(preset_name)
        
        # プリセットデータを取得してドロップダウンを更新
        presets_data = load_presets()
        choices = [preset["name"] for preset in presets_data["presets"]]
        default_presets = [name for name in choices if any(p["name"] == name and p.get("is_default", False) for p in presets_data["presets"])]
        user_presets = [name for name in choices if name not in default_presets]
        sorted_names = sorted(default_presets) + sorted(user_presets)
        updated_choices = [(name, name) for name in sorted_names]
        
        return result, gr.update(choices=updated_choices)

    apply_preset_btn.click(
        fn=apply_to_prompt,
        inputs=[edit_prompt],
        outputs=[prompt]
    )
    
    delete_preset_btn.click(
        fn=delete_preset_handler,
        inputs=[preset_dropdown],
        outputs=[result_message, preset_dropdown]
    )

# 起動コード
block.launch(
    server_name=args.server,
    server_port=args.port,
    share=args.share,
    inbrowser=args.inbrowser,
)