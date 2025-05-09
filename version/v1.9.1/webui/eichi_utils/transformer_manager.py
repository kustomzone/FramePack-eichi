import torch
import traceback
from diffusers_helper.models.hunyuan_video_packed import HunyuanVideoTransformer3DModelPacked
from diffusers_helper.memory import DynamicSwapInstaller
from locales.i18n_extended import translate

class TransformerManager:
    """transformerモデルの状態管理を行うクラス
    
    このクラスは以下の責務を持ちます：
    - transformerモデルのライフサイクル管理
    - LoRA設定の管理
    - FP8最適化の管理
    - モデルモード（通常/F1）の管理
    
    設定の変更はすぐには適用されず、次回のリロード時に適用されます。
    """

    # モデルパス定義
    MODEL_PATH_NORMAL = 'lllyasviel/FramePackI2V_HY'
    MODEL_PATH_F1 = 'lllyasviel/FramePack_F1_I2V_HY_20250503'

    def __init__(self, device, high_vram_mode=False, use_f1_model=False):
        self.transformer = None
        self.device = device

        # 現在適用されている設定
        self.current_state = {
            'lora_path': None,
            'lora_scale': None,
            'fp8_enabled': False,
            'is_loaded': False,
            'high_vram': high_vram_mode,
            'use_f1_model': use_f1_model  # F1モデル使用フラグ
        }

        # 次回のロード時に適用する設定
        self.next_state = self.current_state.copy()
        
    def set_next_settings(self, lora_path=None, lora_scale=None, fp8_enabled=False, high_vram_mode=False, use_f1_model=None):
        """次回のロード時に使用する設定をセット（即時のリロードは行わない）
        
        Args:
            lora_path: LoRAファイルのパス（Noneの場合はLoRA無効）
            lora_scale: LoRAのスケール値（lora_pathがNoneの場合は無視）
            fp8_enabled: FP8最適化の有効/無効（LoRAが設定されている場合のみ有効）
            high_vram_mode: High-VRAMモードの有効/無効
            use_f1_model: F1モデル使用フラグ（Noneの場合は現在の設定を維持）
        """
        # LoRAが設定されていない場合はFP8最適化を強制的に無効化
        actual_fp8_enabled = fp8_enabled if lora_path is not None else False
        
        # F1モデルフラグが指定されていない場合は現在の設定を維持
        actual_use_f1_model = use_f1_model if use_f1_model is not None else self.current_state.get('use_f1_model', False)
        
        self.next_state = {
            'lora_path': lora_path,
            'lora_scale': lora_scale,
            'fp8_enabled': actual_fp8_enabled,
            'high_vram': high_vram_mode,
            'is_loaded': self.current_state['is_loaded'],
            'use_f1_model': actual_use_f1_model
        }
        print(translate("次回のtransformer設定を設定しました:"))
        print(f"  - LoRA: {lora_path if lora_path else 'None'}")
        if lora_path:
            print(f"  - LoRA scale: {lora_scale}")
            print(f"  - FP8 optimization: {actual_fp8_enabled}")
        else:
            print(translate("  - FP8 optimization: 無効 (LoRAが設定されていないため)"))
        print(f"  - High-VRAM mode: {high_vram_mode}")
        print(f"  - F1 Model: {actual_use_f1_model}")
    
    def _needs_reload(self):
        """現在の状態と次回の設定を比較し、リロードが必要かどうかを判断"""
        if not self._is_loaded():
            return True

        # LoRAパスの比較（Noneの場合は特別処理）
        if self.current_state['lora_path'] is None:
            if self.next_state['lora_path'] is not None:
                return True
        elif self.next_state['lora_path'] is None:
            return True
        elif self.current_state['lora_path'] != self.next_state['lora_path']:
            return True

        # LoRAスケールとFP8最適化の比較（LoRAが有効な場合のみ）
        if self.next_state['lora_path'] is not None:
            if self.current_state['lora_scale'] != self.next_state['lora_scale']:
                return True
            if self.current_state['fp8_enabled'] != self.next_state['fp8_enabled']:
                return True

        # High-VRAMモードの比較
        if self.current_state['high_vram'] != self.next_state['high_vram']:
            return True
            
        # F1モデル設定の比較
        if self.current_state.get('use_f1_model', False) != self.next_state.get('use_f1_model', False):
            return True

        return False
    
    def _is_loaded(self):
        """transformerが読み込まれているかどうかを確認"""
        return self.transformer is not None and self.current_state['is_loaded']
    
    def get_transformer(self):
        """現在のtransformerインスタンスを取得"""
        return self.transformer

    def ensure_transformer_state(self):
        """transformerの状態を確認し、必要に応じてリロード"""
        if self._needs_reload():
            print(translate("transformerをリロードします"))
            return self._reload_transformer()        
        print(translate("ロード済みのtransformerを再度利用します"))
        return True
    
    def _reload_transformer(self):
        """next_stateの設定でtransformerをリロード"""
        try:
            # 既存のtransformerモデルを破棄してメモリを解放
            if self.transformer is not None:
                self.current_state['is_loaded'] = False
                # モデルの参照を削除
                del self.transformer
                # 明示的にガベージコレクションを実行
                import gc
                gc.collect()
                # CUDAキャッシュもクリア
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()

            print(translate("\ntransformerをリロードします..."))
            print(translate("適用するtransformer設定:"))
            print(f"  - LoRA: {self.next_state['lora_path'] if self.next_state['lora_path'] else 'None'}")
            if self.next_state['lora_path']:
                print(f"  - LoRA scale: {self.next_state['lora_scale']}")
            print(f"  - FP8 optimization: {self.next_state['fp8_enabled']}")
            print(f"  - High-VRAM mode: {self.next_state['high_vram']}")

            # モードに応じたモデルパスを選択
            model_path = self.MODEL_PATH_F1 if self.next_state.get('use_f1_model', False) else self.MODEL_PATH_NORMAL
            
            # 新しいtransformerインスタンスを作成
            self.transformer = HunyuanVideoTransformer3DModelPacked.from_pretrained(
                model_path,
                torch_dtype=torch.bfloat16
            )

            print(translate("使用モデル: {0}").format(model_path))

            # LoRAとFP8最適化の適用：ピークメモリ使用量を削減するために同時に適用
            if self.next_state['lora_path'] is not None or self.next_state['fp8_enabled']:
                # FP8最適化の適用可能性チェック
                if self.next_state['fp8_enabled']:
                    from lora_utils.fp8_optimization_utils import check_fp8_support
                    has_e4m3, has_e5m2, has_scaled_mm = check_fp8_support()
                    
                    if not has_e4m3:
                        print(translate("FP8最適化が有効化されていますが、サポートされていません。PyTorch 2.1以上が必要です。"))
                        self.next_state['fp8_enabled'] = False

                # 状態辞書を取得
                state_dict = self.transformer.state_dict()

                # LoRAの適用（重みのFP8最適化も必要なら同時に行う）
                if self.next_state['lora_path'] is not None:
                    try:
                        from lora_utils.lora_loader import load_and_apply_lora
                        state_dict = load_and_apply_lora(
                            self.transformer,
                            state_dict,
                            self.next_state['lora_path'],
                            self.next_state['lora_scale'],
                            self.next_state['fp8_enabled'],
                            device=self.device
                        )
                        print(translate("LoRAを直接適用しました (スケール: {0})").format(self.next_state['lora_scale']))
                        
                        # LoRA診断
                        try:
                            from lora_utils.lora_check_helper import check_lora_applied
                            has_lora, source = check_lora_applied(self.transformer)
                            print(translate("LoRA適用状況: {0}, 適用方法: {1}").format(has_lora, source))
                        except Exception as diagnostic_error:
                            print(translate("LoRA診断エラー: {0}").format(diagnostic_error))

                    except Exception as e:
                        print(translate("LoRA適用エラー: {0}").format(e))
                        traceback.print_exc()
                        raise e
                        
                # FP8最適化の適用
                if self.next_state['fp8_enabled']:
                    try:
                        from lora_utils.fp8_optimization_utils import optimize_state_dict_with_fp8, apply_fp8_monkey_patch

                        # LoRAが適用されている場合は、すでにFP8最適化されている
                        if self.next_state['lora_path'] is None:
                            # 最適化のターゲットと除外キーを設定
                            TARGET_KEYS = ["transformer_blocks", "single_transformer_blocks"]
                            EXCLUDE_KEYS = ["norm"]
                            
                            # 状態辞書をFP8形式に最適化
                            print(translate("FP8形式で状態辞書を最適化しています..."))
                            state_dict = optimize_state_dict_with_fp8(
                                state_dict,
                                self.device,
                                TARGET_KEYS,
                                EXCLUDE_KEYS,
                                move_to_device=False
                            )
                            
                        # モンキーパッチの適用
                        print(translate("FP8モンキーパッチを適用しています..."))
                        use_scaled_mm = has_scaled_mm and has_e5m2
                        apply_fp8_monkey_patch(self.transformer, state_dict, use_scaled_mm=use_scaled_mm)
                        
                        print(translate("FP8最適化が適用されました！"))
                    except Exception as e:
                        print(translate("FP8最適化エラー: {0}").format(e))
                        traceback.print_exc()
                        raise e
                
                # 必要に応じてLoRA、FP8最適化が施された状態辞書を読み込み
                self.transformer.load_state_dict(state_dict, strict=True)
            
            self.transformer.cpu()
            self.transformer.eval()
            self.transformer.high_quality_fp32_output_for_inference = True
            print('transformer.high_quality_fp32_output_for_inference = True')
            self.transformer.to(dtype=torch.bfloat16)
            self.transformer.requires_grad_(False)
            
            # VRAMモードに応じた設定
            if not self.next_state['high_vram']:
                DynamicSwapInstaller.install_model(self.transformer, device=self.device)
            else:
                self.transformer.to(self.device)
            
            # 状態を更新
            self.next_state['is_loaded'] = True
            self.current_state = self.next_state.copy()
            
            print(translate("transformerのリロードが完了しました"))
            return True
            
        except Exception as e:
            print(translate("transformerリロードエラー: {0}").format(e))
            traceback.print_exc()
            self.current_state['is_loaded'] = False
            return False
