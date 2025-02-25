import matplotlib
matplotlib.use('Agg')

from utils import audio
import matplotlib.pyplot as plt
from data_gen.tts.data_gen_utils import get_pitch
from tasks.tts.fs2_utils import FastSpeechDataset
from modules.commons.ssim import ssim
import os
from multiprocessing.pool import Pool
from tqdm import tqdm
from modules.fastspeech.tts_modules import mel2ph_to_dur
from utils.hparams import hparams
from utils.plot import spec_to_figure, dur_to_figure, f0_to_figure
from utils.pitch_utils import denorm_f0
from modules.fastspeech.fs2 import FastSpeech2
from tasks.tts.tts import TtsTask
import torch
import torch.optim
import torch.utils.data
import torch.nn.functional as F
import utils
import torch.distributions
import numpy as np
from modules.diffsinger_midi.fs2 import FastSpeech2MIDI
from modules.fastspeech.pe import PitchExtractor
from tasks.base_task import BaseTask, BaseDataset
from tasks.base_task import data_loader
import json
from utils.text_encoder import TokenTextEncoder
from utils.common_schedulers import RSQRTSchedule
from vocoders.base_vocoder import get_vocoder_cls, BaseVocoder

class MIDIDataset(FastSpeechDataset):
    def __getitem__(self, index):
        sample = super(MIDIDataset, self).__getitem__(index)
        item = self._get_item(index)
        if len(item['pitch_midi']) != len(item['phone']):
            print(len(item['pitch_midi']), len(item['phone']))
            print(item['phone'])
            print(item['item_name'])
        sample['pitch_midi'] = torch.LongTensor(item['pitch_midi'])[:hparams['max_frames']]
        sample['midi_dur'] = torch.FloatTensor(item['midi_dur'])[:hparams['max_frames']]
        sample['is_slur'] = torch.LongTensor(item['is_slur'])[:hparams['max_frames']]
        sample['word_boundary'] = torch.LongTensor(item['word_boundary'])[:hparams['max_frames']]
        return sample

    def collater(self, samples):
        batch = super(MIDIDataset, self).collater(samples)
        batch['pitch_midi'] = utils.collate_1d([s['pitch_midi'] for s in samples], 0)
        batch['midi_dur'] = utils.collate_1d([s['midi_dur'] for s in samples], 0)
        batch['is_slur'] = utils.collate_1d([s['is_slur'] for s in samples], 0)
        batch['word_boundary'] = utils.collate_1d([s['word_boundary'] for s in samples], 0)
        return batch

class FastSpeech2Task(BaseTask):
    def __init__(self):
        super(FastSpeech2Task, self).__init__()
        self.max_tokens = hparams['max_tokens']
        self.max_sentences = hparams['max_sentences']
        self.max_eval_tokens = hparams['max_eval_tokens']
        if self.max_eval_tokens == -1:
            hparams['max_eval_tokens'] = self.max_eval_tokens = self.max_tokens
        self.max_eval_sentences = hparams['max_eval_sentences']
        if self.max_eval_sentences == -1:
            hparams['max_eval_sentences'] = self.max_eval_sentences = self.max_sentences
        self.vocoder = None
        self.phone_encoder = self.build_phone_encoder(hparams['binary_data_dir'])
        self.padding_idx = self.phone_encoder.pad()
        self.eos_idx = self.phone_encoder.eos()
        self.seg_idx = self.phone_encoder.seg()
        self.saving_result_pool = None
        self.saving_results_futures = None
        self.stats = {}

        if hparams.get('use_midi') is not None and hparams['use_midi']:
            self.dataset_cls = MIDIDataset
        else:
            self.dataset_cls = FastSpeechDataset
        self.mse_loss_fn = torch.nn.MSELoss()
        mel_losses = hparams['mel_loss'].split("|")
        self.loss_and_lambda = {}
        for i, l in enumerate(mel_losses):
            if l == '':
                continue
            if ':' in l:
                l, lbd = l.split(":")
                lbd = float(lbd)
            else:
                lbd = 1.0
            self.loss_and_lambda[l] = lbd
        print("| Mel losses:", self.loss_and_lambda)
        # ['<pad>', '<EOS>', '<UNK>', ',', '.', '<BOS>', '|', SP, AP]
        self.sil_ph = self.phone_encoder.sil_phonemes()

        if hparams.get('pe_enable') is not None and hparams['pe_enable']:
            self.pe = PitchExtractor(conv_layers=2).cuda()
            utils.load_ckpt(self.pe, hparams['pe_ckpt'])
            self.pe.eval()


    @data_loader
    def train_dataloader(self):
        train_dataset = self.dataset_cls(prefix=hparams['train_set_name'], shuffle=True)
        return self.build_dataloader(train_dataset, True, self.max_tokens, self.max_sentences,
                                     endless=hparams['endless_ds'])

    @data_loader
    def val_dataloader(self):
        valid_dataset = self.dataset_cls(prefix=hparams['valid_set_name'], shuffle=False)
        return self.build_dataloader(valid_dataset, False, self.max_eval_tokens, self.max_eval_sentences)

    @data_loader
    def test_dataloader(self):
        test_dataset = self.dataset_cls(prefix=hparams['test_set_name'], shuffle=False)
        self.test_dl = self.build_dataloader(
            test_dataset, False, self.max_eval_tokens,
            self.max_eval_sentences, batch_by_size=False)
        return self.test_dl


    def build_tts_model(self):
        if hparams.get('use_midi') is not None and hparams['use_midi']:
            self.model = FastSpeech2MIDI(self.phone_encoder)
        else:
            self.model = FastSpeech2(self.phone_encoder)

    def build_phone_encoder(self, data_dir):
        phone_list_file = os.path.join(data_dir, 'phone_set.json')
        phone_list = json.load(open(phone_list_file))
        return TokenTextEncoder(None, vocab_list=phone_list, replace_oov=',')

    def build_scheduler(self, optimizer):
        return RSQRTSchedule(optimizer)

    def build_optimizer(self, model):
        self.optimizer = optimizer = torch.optim.AdamW(
            model.parameters(),
            lr=hparams['lr'],
            betas=(hparams['optimizer_adam_beta1'], hparams['optimizer_adam_beta2']),
            weight_decay=hparams['weight_decay'])
        return optimizer

    def build_model(self):
        self.build_tts_model()
        if hparams['load_ckpt'] != '':
            self.load_ckpt(hparams['load_ckpt'], strict=True)
        utils.print_arch(self.model)
        return self.model

    def _training_step(self, sample, batch_idx, _):
        loss_output = self.run_model(self.model, sample)
        total_loss = sum([v for v in loss_output.values() if isinstance(v, torch.Tensor) and v.requires_grad])
        loss_output['batch_size'] = sample['txt_tokens'].size()[0]
        return total_loss, loss_output

    def build_dataloader(self, dataset, shuffle, max_tokens=None, max_sentences=None,
                         required_batch_size_multiple=-1, endless=False, batch_by_size=True):
        devices_cnt = torch.cuda.device_count()
        if devices_cnt == 0:
            devices_cnt = 1
        if required_batch_size_multiple == -1:
            required_batch_size_multiple = devices_cnt

        def shuffle_batches(batches):
            np.random.shuffle(batches)
            return batches

        if max_tokens is not None:
            max_tokens *= devices_cnt
        if max_sentences is not None:
            max_sentences *= devices_cnt
        indices = dataset.ordered_indices()
        if batch_by_size:
            batch_sampler = utils.batch_by_size(
                indices, dataset.num_tokens, max_tokens=max_tokens, max_sentences=max_sentences,
                required_batch_size_multiple=required_batch_size_multiple,
            )
        else:
            batch_sampler = []
            for i in range(0, len(indices), max_sentences):
                batch_sampler.append(indices[i:i + max_sentences])

        if shuffle:
            batches = shuffle_batches(list(batch_sampler))
            if endless:
                batches = [b for _ in range(1000) for b in shuffle_batches(list(batch_sampler))]
        else:
            batches = batch_sampler
            if endless:
                batches = [b for _ in range(1000) for b in batches]
        num_workers = dataset.num_workers
        if self.trainer.use_ddp:
            num_replicas = dist.get_world_size()
            rank = dist.get_rank()
            batches = [x[rank::num_replicas] for x in batches if len(x) % num_replicas == 0]
        return torch.utils.data.DataLoader(dataset,
                                           collate_fn=dataset.collater,
                                           batch_sampler=batches,
                                           num_workers=num_workers,
                                           pin_memory=False)


    def validation_step(self, sample, batch_idx):
        outputs = {}
        outputs['losses'] = {}
        outputs['losses'], model_out = self.run_model(self.model, sample, return_output=True)
        outputs['total_loss'] = sum(outputs['losses'].values())
        outputs['nsamples'] = sample['nsamples']
        mel_out = self.model.out2mel(model_out['mel_out'])
        outputs = utils.tensors_to_scalars(outputs)
        if batch_idx < hparams['num_valid_plots']:
            self.plot_mel(batch_idx, sample['mels'], mel_out)
            self.plot_dur(batch_idx, sample, model_out)
            if hparams['use_pitch_embed']:
                self.plot_pitch(batch_idx, sample, model_out)
        return outputs

    def _validation_end(self, outputs):
        all_losses_meter = {
            'total_loss': utils.AvgrageMeter(),
        }
        for output in outputs:
            n = output['nsamples']
            for k, v in output['losses'].items():
                if k not in all_losses_meter:
                    all_losses_meter[k] = utils.AvgrageMeter()
                all_losses_meter[k].update(v, n)
            all_losses_meter['total_loss'].update(output['total_loss'], n)
        return {k: round(v.avg, 4) for k, v in all_losses_meter.items()}

    def run_model(self, model, sample, return_output=False):
        txt_tokens = sample['txt_tokens']
        target = sample['mels']
        mel2ph = sample['mel2ph']
        f0 = sample['f0']
        uv = sample['uv']
        energy = sample['energy']
        spk_embed = sample.get('spk_embed') if not hparams['use_spk_id'] else sample.get('spk_ids') #[4]
        if hparams.get('use_midi') is not None and hparams['use_midi']:
            output = self.model(
                txt_tokens, spk_embed=spk_embed, mel2ph=mel2ph, f0=f0, uv=uv, ref_mels=target, infer=False,
                pitch_midi=sample['pitch_midi'], midi_dur=sample.get('midi_dur'), is_slur=sample.get('is_slur'),
                energy=energy)
        else:
            output = model(txt_tokens, mel2ph=mel2ph, spk_embed=spk_embed,
                           ref_mels=target, f0=f0, uv=uv, energy=energy, infer=False)
        losses = {}
        self.add_mel_loss(output['mel_out'], target, losses)
        self.add_dur_loss(output['dur'], mel2ph, txt_tokens, sample['word_boundary'], losses=losses)
        if hparams['use_pitch_embed']:
            self.add_pitch_loss(output, sample, losses)
        if not return_output:
            return losses
        else:
            return losses, output

    ############
    # losses
    ############
    def add_mel_loss(self, mel_out, target, losses, postfix='', mel_mix_loss=None):
        nonpadding = target.abs().sum(-1).ne(0).float()
        for loss_name, lbd in self.loss_and_lambda.items():
            if 'l1' == loss_name:
                l = self.l1_loss(mel_out, target)
            elif 'mse' == loss_name:
                l = self.mse_loss(mel_out, target)
            elif 'ssim' == loss_name:
                l = self.ssim_loss(mel_out, target)
            elif 'gdl' == loss_name:
                l = self.gdl_loss_fn(mel_out, target, nonpadding) \
                    * self.loss_and_lambda['gdl']
            losses[f'{loss_name}{postfix}'] = l * lbd

    def l1_loss(self, decoder_output, target):
        # decoder_output : B x T x n_mel
        # target : B x T x n_mel
        l1_loss = F.l1_loss(decoder_output, target, reduction='none')
        weights = self.weights_nonzero_speech(target)
        l1_loss = (l1_loss * weights).sum() / weights.sum()
        return l1_loss

    def mse_loss(self, decoder_output, target):
        # decoder_output : B x T x n_mel
        # target : B x T x n_mel
        assert decoder_output.shape == target.shape
        mse_loss = F.mse_loss(decoder_output, target, reduction='none')
        weights = self.weights_nonzero_speech(target)
        mse_loss = (mse_loss * weights).sum() / weights.sum()
        return mse_loss

    def ssim_loss(self, decoder_output, target, bias=6.0):
        # decoder_output : B x T x n_mel
        # target : B x T x n_mel
        assert decoder_output.shape == target.shape
        weights = self.weights_nonzero_speech(target)
        decoder_output = decoder_output[:, None] + bias
        target = target[:, None] + bias
        ssim_loss = 1 - ssim(decoder_output, target, size_average=False)
        ssim_loss = (ssim_loss * weights).sum() / weights.sum()
        return ssim_loss


    def add_dur_loss(self, dur_pred, mel2ph, txt_tokens, wdb, losses=None):
        """
        :param dur_pred: [B, T], float, log scale
        :param mel2ph: [B, T]
        :param txt_tokens: [B, T]
        :param losses:
        :return:
        """
        B, T = txt_tokens.shape
        nonpadding = (txt_tokens != 0).float()
        dur_gt = mel2ph_to_dur(mel2ph, T).float() * nonpadding
        is_sil = torch.zeros_like(txt_tokens).bool()
        for p in self.sil_ph:
            is_sil = is_sil | (txt_tokens == self.phone_encoder.encode(p)[0])
        is_sil = is_sil.float()  # [B, T_txt]

        if hparams['lambda_ph_dur'] > 0:
            # phone duration loss
            if hparams['dur_loss'] == 'mse':
                losses['pdur'] = F.mse_loss(dur_pred, (dur_gt + 1).log(), reduction='none')
                losses['pdur'] = (losses['pdur'] * nonpadding).sum() / nonpadding.sum()
                dur_pred = (dur_pred.exp() - 1).clamp(min=0)
            else:
                raise NotImplementedError

        # use linear scale for sent and word duration
        if hparams['lambda_word_dur'] > 0:
            idx = F.pad(wdb.cumsum(axis=1), (1, 0))[:, :-1]
            # word_dur_g = dur_gt.new_zeros([B, idx.max() + 1]).scatter_(1, idx, midi_dur)  # midi_dur can be implied by add gt-ph_dur
            word_dur_p = dur_pred.new_zeros([B, idx.max() + 1]).scatter_add(1, idx, dur_pred)
            word_dur_g = dur_gt.new_zeros([B, idx.max() + 1]).scatter_add(1, idx, dur_gt)
            wdur_loss = F.mse_loss((word_dur_p + 1).log(), (word_dur_g + 1).log(), reduction='none')
            word_nonpadding = (word_dur_g > 0).float()
            wdur_loss = (wdur_loss * word_nonpadding).sum() / word_nonpadding.sum()
            losses['wdur'] = wdur_loss * hparams['lambda_word_dur']
        if hparams['lambda_sent_dur'] > 0:
            sent_dur_p = dur_pred.sum(-1)
            sent_dur_g = dur_gt.sum(-1)
            sdur_loss = F.mse_loss((sent_dur_p + 1).log(), (sent_dur_g + 1).log(), reduction='mean')
            losses['sdur'] = sdur_loss.mean() * hparams['lambda_sent_dur']

    def add_pitch_loss(self, output, sample, losses):
        if hparams['pitch_type'] == 'ph':
            nonpadding = (sample['txt_tokens'] != 0).float()
            pitch_loss_fn = F.l1_loss if hparams['pitch_loss'] == 'l1' else F.mse_loss
            losses['f0'] = (pitch_loss_fn(output['pitch_pred'][:, :, 0], sample['f0'],
                                          reduction='none') * nonpadding).sum() \
                           / nonpadding.sum() * hparams['lambda_f0']
            return
        mel2ph = sample['mel2ph']  # [B, T_s]
        f0 = sample['f0']
        uv = sample['uv']
        nonpadding = (mel2ph != 0).float()
        self.add_f0_loss(output['pitch_pred'], f0, uv, losses, nonpadding=nonpadding)

    def add_f0_loss(self, p_pred, f0, uv, losses, nonpadding, postfix=''):
        assert p_pred[..., 0].shape == f0.shape
        if hparams['use_uv']:
            assert p_pred[..., 1].shape == uv.shape
            losses[f'uv{postfix}'] = (F.binary_cross_entropy_with_logits(
                p_pred[:, :, 1], uv, reduction='none') * nonpadding).sum() \
                                     / nonpadding.sum() * hparams['lambda_uv']
            nonpadding = nonpadding * (uv == 0).float()
        f0_pred = p_pred[:, :, 0]
        pitch_loss_fn = F.l1_loss if hparams['pitch_loss'] == 'l1' else F.mse_loss
        losses[f'f0{postfix}'] = (pitch_loss_fn(f0_pred, f0, reduction='none') * nonpadding).sum() \
                                 / nonpadding.sum() * hparams['lambda_f0']

    ############
    # validation plots
    ############
    def plot_mel(self, batch_idx, spec, spec_out, name=None):
        spec_cat = torch.cat([spec, spec_out], -1)
        name = f'mel_{batch_idx}' if name is None else name
        vmin = hparams['mel_vmin']
        vmax = hparams['mel_vmax']
        self.logger.add_figure(name, spec_to_figure(spec_cat[0], vmin, vmax), self.global_step)

    def plot_dur(self, batch_idx, sample, model_out):
        T_txt = sample['txt_tokens'].shape[1]
        dur_gt = mel2ph_to_dur(sample['mel2ph'], T_txt)[0]
        dur_pred = self.model.dur_predictor.out2dur(model_out['dur']).float()
        txt = self.phone_encoder.decode(sample['txt_tokens'][0].cpu().numpy())
        txt = txt.split(" ")
        self.logger.add_figure(
            f'dur_{batch_idx}', dur_to_figure(dur_gt, dur_pred, txt), self.global_step)

    def plot_pitch(self, batch_idx, sample, model_out):
        f0 = sample['f0']
        f0 = denorm_f0(f0, sample['uv'], hparams)
        uv_pred = model_out['pitch_pred'][:, :, 1] > 0
        pitch_pred = denorm_f0(model_out['pitch_pred'][:, :, 0], uv_pred, hparams)
        self.logger.add_figure(
            f'f0_{batch_idx}', f0_to_figure(f0[0], None, pitch_pred[0]), self.global_step)

    ############
    # infer
    ############
    def test_start(self):
        self.saving_result_pool = Pool(8)
        self.saving_results_futures = []

        self.vocoder: BaseVocoder = get_vocoder_cls(hparams)()

    def test_step(self, sample, batch_idx):
        spk_embed = sample.get('spk_embed') if not hparams['use_spk_id'] else sample.get('spk_ids')
        txt_tokens = sample['txt_tokens']
        mel2ph, uv, f0 = None, None, None
        ref_mels = sample['mels']
        if hparams['use_gt_dur']:
            mel2ph = sample['mel2ph']
        if hparams['use_gt_f0']:
            f0 = sample['f0']
            uv = sample['uv']
        if hparams.get('use_midi') is not None and hparams['use_midi']:
            outputs = self.model(
                txt_tokens, spk_embed=spk_embed, mel2ph=mel2ph, f0=f0, uv=uv, ref_mels=ref_mels, infer=True,
                pitch_midi=sample['pitch_midi'], midi_dur=sample.get('midi_dur'), is_slur=sample.get('is_slur'))
        else:
            outputs = self.model(
                txt_tokens, spk_embed=spk_embed, mel2ph=mel2ph, f0=f0, uv=uv, ref_mels=ref_mels, infer=True)
        sample['outputs'] = self.model.out2mel(outputs['mel_out'])
        sample['mel2ph_pred'] = outputs['mel2ph']
        # if hparams['use_pitch_embed']:
        if hparams.get('pe_enable') is not None and hparams['pe_enable']:
            sample['f0'] = self.pe(sample['mels'])['f0_denorm_pred']  # pe predict from GT mel
            sample['f0_pred'] = self.pe(outputs['mel_out'])['f0_denorm_pred']  # pe predict from Pred mel
        else:
            sample['f0'] = denorm_f0(sample['f0'], sample['uv'], hparams)
            sample['f0_pred'] = outputs.get('f0_denorm')

        return self.after_infer(sample)

    def after_infer(self, predictions, sil_start_frame=0):
        if self.saving_result_pool is None and not hparams['profile_infer']:
            self.saving_result_pool = Pool(min(int(os.getenv('N_PROC', os.cpu_count())), 16))
            self.saving_results_futures = []
        predictions = utils.unpack_dict_to_list(predictions)
        t = tqdm(predictions)
        for num_predictions, prediction in enumerate(t):
            for k, v in prediction.items():
                if type(v) is torch.Tensor:
                    prediction[k] = v.cpu().numpy()

            item_name = prediction.get('item_name')
            text = prediction.get('text').replace(":", "%3A")[:80]

            # remove paddings
            mel_gt = prediction["mels"]
            mel_gt_mask = np.abs(mel_gt).sum(-1) > 0
            mel_gt = mel_gt[mel_gt_mask]
            mel2ph_gt = prediction.get("mel2ph")
            mel2ph_gt = mel2ph_gt[mel_gt_mask] if mel2ph_gt is not None else None
            mel_pred = prediction["outputs"]
            mel_pred_mask = np.abs(mel_pred).sum(-1) > 0
            mel_pred = mel_pred[mel_pred_mask]
            # mel_gt = np.clip(mel_gt, hparams['mel_vmin'], hparams['mel_vmax']) # no clip
            # mel_pred = np.clip(mel_pred, hparams['mel_vmin'], hparams['mel_vmax'])

            mel2ph_pred = prediction.get("mel2ph_pred")
            if mel2ph_pred is not None:
                if len(mel2ph_pred) > len(mel_pred_mask):
                    mel2ph_pred = mel2ph_pred[:len(mel_pred_mask)]
                mel2ph_pred = mel2ph_pred[mel_pred_mask]

            f0_gt = prediction.get("f0")
            f0_pred = prediction.get("f0_pred")
            f0_gt = f0_gt[mel_gt_mask]
            if f0_pred is not None:
                f0_gt = f0_gt[mel_gt_mask]
                if len(f0_pred) > len(mel_pred_mask):
                    f0_pred = f0_pred[:len(mel_pred_mask)]
                f0_pred = f0_pred[mel_pred_mask]

            str_phs = None
            if self.phone_encoder is not None and 'txt_tokens' in prediction:
                str_phs = self.phone_encoder.decode(prediction['txt_tokens'], strip_padding=True)
            gen_dir = os.path.join(hparams['work_dir'],
                                   f'generated_{self.trainer.global_step}_{hparams["gen_dir_name"]}')
            wav_pred = self.vocoder.spec2wav(mel_pred, f0=f0_pred)
            wav_pred[:sil_start_frame * hparams['hop_size']] = 0
            if not hparams['profile_infer']:
                os.makedirs(gen_dir, exist_ok=True)
                os.makedirs(f'{gen_dir}/wavs', exist_ok=True)
                os.makedirs(f'{gen_dir}/plot', exist_ok=True)
                if hparams.get('save_mel_npy', False):
                    os.makedirs(f'{gen_dir}/npy', exist_ok=True)
                self.saving_results_futures.append(
                    self.saving_result_pool.apply_async(self.save_result, args=[
                        wav_pred, mel_pred, 'P', item_name, text, gen_dir, str_phs, mel2ph_pred]))

                if mel_gt is not None and hparams['save_gt']:
                    wav_gt = self.vocoder.spec2wav(mel_gt, f0=f0_gt)
                    self.saving_results_futures.append(
                        self.saving_result_pool.apply_async(self.save_result, args=[
                            wav_gt, mel_gt, 'G', item_name, text, gen_dir, str_phs, mel2ph_gt]))
                    if hparams['save_f0']:
                        import matplotlib.pyplot as plt
                        f0_pred_, _ = get_pitch(wav_pred, mel_pred, hparams)
                        f0_gt_, _ = get_pitch(wav_gt, mel_gt, hparams)
                        fig = plt.figure()
                        plt.plot(f0_pred_, label=r'$\hat{f_0}$')
                        plt.plot(f0_gt_, label=r'$f_0$')
                        plt.legend()
                        plt.tight_layout()
                        plt.savefig(f'{gen_dir}/plot/[F0][{item_name}]{text}.png', format='png')
                        plt.close(fig)

                t.set_description(
                    f"Pred_shape: {mel_pred.shape}, gt_shape: {mel_gt.shape}")
            else:
                if 'gen_wav_time' not in self.stats:
                    self.stats['gen_wav_time'] = 0
                self.stats['gen_wav_time'] += len(wav_pred) / hparams['audio_sample_rate']
                print('gen_wav_time: ', self.stats['gen_wav_time'])
            import time
            time.sleep(1)
        return {}

    @staticmethod
    def save_result(wav_out, mel, prefix, item_name, text, gen_dir, str_phs=None, mel2ph=None):
        base_fn = f'[{item_name}][{prefix}]'
        if str_phs is not None:
            base_fn += str_phs
        audio.save_wav(wav_out, f'{gen_dir}/wavs/{base_fn}.wav', hparams['audio_sample_rate'],
                       norm=hparams['out_wav_norm'])

        fig = plt.figure(figsize=(14, 10))
        spec_vmin = hparams['mel_vmin']
        spec_vmax = hparams['mel_vmax']
        heatmap = plt.pcolor(mel.T, vmin=spec_vmin, vmax=spec_vmax)
        fig.colorbar(heatmap)
        f0, _ = get_pitch(wav_out, mel, hparams)
        f0 = f0 / 10 * (f0 > 0)
        plt.plot(f0, c='white', linewidth=1, alpha=0.6)
        if mel2ph is not None and str_phs is not None:
            decoded_txt = str_phs.split(" ")
            dur = mel2ph_to_dur(torch.LongTensor(mel2ph)[None, :], len(decoded_txt))[0].numpy()
            dur = [0] + list(np.cumsum(dur))
            for i in range(len(dur) - 1):
                shift = (i % 20) + 1
                plt.text(dur[i], shift, decoded_txt[i])
                plt.hlines(shift, dur[i], dur[i + 1], colors='b' if decoded_txt[i] != '|' else 'black')
                plt.vlines(dur[i], 0, 5, colors='b' if decoded_txt[i] != '|' else 'black',
                           alpha=1, linewidth=1)
        plt.tight_layout()
        plt.savefig(f'{gen_dir}/plot/{base_fn}.png', format='png')
        plt.close(fig)
        if hparams.get('save_mel_npy', False):
            np.save(f'{gen_dir}/npy/{base_fn}', mel)

    def test_end(self, outputs):
        self.saving_result_pool.close()
        [f.get() for f in tqdm(self.saving_results_futures)]
        self.saving_result_pool.join()
        return {}

    ##########
    # utils
    ##########
    def remove_padding(self, x, padding_idx=0):
        return utils.remove_padding(x, padding_idx)

    def weights_nonzero_speech(self, target):
        # target : B x T x mel
        # Assign weight 1.0 to all labels except for padding (id=0).
        dim = target.size(-1)
        return target.abs().sum(-1, keepdim=True).ne(0).float().repeat(1, 1, dim)

    def make_stop_target(self, target):
        # target : B x T x mel
        seq_mask = target.abs().sum(-1).ne(0).float()
        seq_length = seq_mask.sum(1)
        mask_r = 1 - sequence_mask(seq_length - 1, target.size(1)).float()
        return seq_mask, mask_r

    def weighted_cross_entropy_with_logits(self, targets, logits, pos_weight=1):
        x = logits
        z = targets
        q = pos_weight
        l = 1 + (q - 1) * z
        return (1 - z) * x + l * (torch.log(1 + torch.exp(-x.abs())) + F.relu(-x))