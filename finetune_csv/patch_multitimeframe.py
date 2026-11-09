# Patch the finetune_csv pipeline for multi-timeframe (multi-CSV) training.
# Run ON the Windows host: D:\Kronos\.venv-gold\Scripts\python.exe patch_multitimeframe.py
# Changes (minimal, backward compatible - a single CSV still works):
#  1. finetune_base_model.py: CustomKlineDataset accepts one CSV or a list of CSVs.
#     Each CSV is an independent series: splits are per-series in time order,
#     windows are sampled only within a single series, never across timeframes.
#     max_samples_per_series caps windows per series (stride-sampled over the
#     whole series, so the capped subset still spans the full time range).
#  2. finetune_base_model.py + finetune_tokenizer.py: create_dataloaders passes
#     max_samples_per_series (train) / max_val_samples_per_series (val) through.
#  3. config_loader.py: data.data_paths (list) takes precedence over data_path;
#     reads max_samples_per_series / max_val_samples_per_series.
import re
import sys

BASE = r"D:\Kronos\finetune_csv"

NEW_CLASS = '''class CustomKlineDataset(Dataset):
    """K-line dataset supporting one CSV (legacy) or a list of CSVs.

    Multi-series mode (one CSV per timeframe): each CSV is treated as an
    independent series. Train/val/test splits are applied per series in
    time order, and sliding windows are sampled only within a single
    series, so windows never cross timeframes. A series shorter than
    lookback+predict+1 contributes zero samples (e.g. MN1 with 155 bars).
    `max_samples_per_series` caps the number of windows taken from each
    series (stride-sampled across the whole series, so the capped subset
    still spans the full time range); it both balances the timeframes and
    bounds the training set size.
    """

    def __init__(self, data_path, data_type='train', lookback_window=90, predict_window=10,
                 clip=5.0, seed=100, train_ratio=0.7, val_ratio=0.15, test_ratio=0.15,
                 max_samples_per_series=None):
        # data_path: a single CSV path (legacy) or a list/tuple of CSV paths.
        if isinstance(data_path, (list, tuple)):
            self.data_paths = list(data_path)
        else:
            self.data_paths = [data_path]
        self.data_type = data_type
        self.lookback_window = lookback_window
        self.predict_window = predict_window
        self.window = lookback_window + predict_window + 1
        self.clip = clip
        self.seed = seed
        self.train_ratio = train_ratio
        self.val_ratio = val_ratio
        self.test_ratio = test_ratio
        self.max_samples_per_series = max_samples_per_series

        self.feature_list = ['open', 'high', 'low', 'close', 'volume', 'amount']
        self.time_feature_list = ['minute', 'hour', 'weekday', 'day', 'month']

        self.py_rng = random.Random(seed)

        self._load_and_preprocess_data()
        self._split_data_by_time()
        self._build_window_index()

        print(f"[{data_type.upper()}] Total available samples: {self.n_samples}")
        for s, starts in zip(self.series, self.series_starts):
            print(f"[{data_type.upper()}]   {s['name']}: {len(s['data'])} bars -> {len(starts)} samples")

    def _load_and_preprocess_data(self):
        self.series = []
        for path in self.data_paths:
            df = pd.read_csv(path)
            df['timestamps'] = pd.to_datetime(df['timestamps'])
            df = df.sort_values('timestamps').reset_index(drop=True)
            timestamps = df['timestamps'].copy()
            df['minute'] = df['timestamps'].dt.minute
            df['hour'] = df['timestamps'].dt.hour
            df['weekday'] = df['timestamps'].dt.weekday
            df['day'] = df['timestamps'].dt.day
            df['month'] = df['timestamps'].dt.month
            data = df[self.feature_list + self.time_feature_list].copy()
            if data.isnull().any().any():
                print(f"Warning: Missing values found in {path}, performing forward fill")
                data = data.fillna(method='ffill')
            name = os.path.splitext(os.path.basename(path))[0]
            self.series.append({'name': name, 'path': path, 'data': data, 'timestamps': timestamps})
            print(f"Loaded {name}: {len(df)} records, {timestamps.min()} to {timestamps.max()}")

    def _split_data_by_time(self):
        for s in self.series:
            total_length = len(s['data'])
            train_end = int(total_length * self.train_ratio)
            val_end = int(total_length * (self.train_ratio + self.val_ratio))
            if self.data_type == 'train':
                s['data'] = s['data'].iloc[:train_end].copy()
                s['timestamps'] = s['timestamps'].iloc[:train_end].copy()
            elif self.data_type == 'val':
                s['data'] = s['data'].iloc[train_end:val_end].copy()
                s['timestamps'] = s['timestamps'].iloc[train_end:val_end].copy()
            elif self.data_type == 'test':
                s['data'] = s['data'].iloc[val_end:].copy()
                s['timestamps'] = s['timestamps'].iloc[val_end:].copy()
            if len(s['data']):
                print(f"[{self.data_type.upper()}] {s['name']}: {len(s['data'])} records after split "
                      f"({s['timestamps'].min()} to {s['timestamps'].max()})")
            else:
                print(f"[{self.data_type.upper()}] {s['name']}: 0 records after split")

    def _build_window_index(self):
        # Per-series allowed window start positions. When capped, stride-sample
        # across the whole series so the subset still covers the full range.
        self.series_starts = []
        self.series_offsets = []
        total = 0
        for s in self.series:
            n_win = max(0, len(s['data']) - self.window + 1)
            if self.max_samples_per_series is not None and n_win > self.max_samples_per_series:
                stride = n_win / self.max_samples_per_series
                starts = (np.arange(self.max_samples_per_series, dtype=np.float64) * stride).astype(np.int64)
            else:
                starts = np.arange(n_win, dtype=np.int64)
            self.series_starts.append(starts)
            self.series_offsets.append(total)
            total += len(starts)
        self.n_samples = total
        self.series_offsets_arr = np.array(self.series_offsets, dtype=np.int64)

    def set_epoch_seed(self, epoch):
        epoch_seed = self.seed + epoch
        self.py_rng.seed(epoch_seed)
        self.current_epoch = epoch

    def __len__(self):
        return self.n_samples

    def __getitem__(self, idx):
        if self.n_samples == 0:
            raise ValueError("Data length insufficient to create samples")

        if self.data_type == 'train':
            epoch = getattr(self, 'current_epoch', 0)
            idx = (idx * 9973 + (epoch + 1) * 104729) % self.n_samples
        else:
            idx = idx % self.n_samples

        series_idx = int(np.searchsorted(self.series_offsets_arr, idx, side='right')) - 1
        local_idx = idx - self.series_offsets[series_idx]
        start_idx = int(self.series_starts[series_idx][local_idx])
        data = self.series[series_idx]['data']

        end_idx = start_idx + self.window
        window_data = data.iloc[start_idx:end_idx]

        x = window_data[self.feature_list].values.astype(np.float32)
        x_stamp = window_data[self.time_feature_list].values.astype(np.float32)

        x_mean, x_std = np.mean(x, axis=0), np.std(x, axis=0)
        x = (x - x_mean) / (x_std + 1e-5)
        x = np.clip(x, -self.clip, self.clip)

        x_tensor = torch.from_numpy(x)
        x_stamp_tensor = torch.from_numpy(x_stamp)

        return x_tensor, x_stamp_tensor
'''


def replace_class(text):
    start = text.index('class CustomKlineDataset(Dataset):')
    end = text.index('\ndef setup_logging', start)
    return text[:start] + NEW_CLASS + text[end:]


def insert_kwarg(text, varname, kwarg):
    """Insert `kwarg` into the CustomKlineDataset(...) call assigned to varname."""
    m = re.search(re.escape('    ' + varname + ' = CustomKlineDataset('), text)
    assert m, 'constructor call not found: ' + varname
    close = text.index('\n    )', m.end())
    return text[:close] + ',\n        ' + kwarg + text[close:]


def patch_base_model(text):
    text = replace_class(text)
    text = insert_kwarg(text, 'train_dataset',
                        "max_samples_per_series=getattr(config, 'max_samples_per_series', None)")
    text = insert_kwarg(text, 'val_dataset',
                        "max_samples_per_series=getattr(config, 'max_val_samples_per_series', None)")
    return text


def patch_tokenizer(text):
    text = insert_kwarg(text, 'train_dataset',
                        "max_samples_per_series=getattr(config, 'max_samples_per_series', None)")
    text = insert_kwarg(text, 'val_dataset',
                        "max_samples_per_series=getattr(config, 'max_val_samples_per_series', None)")
    return text


def patch_config_loader(text):
    old = "        self.data_path = data_config.get('data_path')"
    assert text.count(old) == 1, 'data_path line not unique'
    new = ("        # data_paths (list) takes precedence over data_path (single file);\n"
           "        # data_path may therefore be a str or a list of str.\n"
           "        self.data_path = data_config.get('data_paths', data_config.get('data_path'))\n"
           "        self.max_samples_per_series = data_config.get('max_samples_per_series')\n"
           "        self.max_val_samples_per_series = data_config.get('max_val_samples_per_series')")
    text = text.replace(old, new)
    old2 = "            'data_path': self.data_path,"
    assert text.count(old2) == 2, 'data_path dict entries: %d' % text.count(old2)
    new2 = ("            'data_path': self.data_path,\n"
            "            'max_samples_per_series': self.max_samples_per_series,\n"
            "            'max_val_samples_per_series': self.max_val_samples_per_series,")
    text = text.replace(old2, new2)
    return text


def patch_file(path, fn):
    with open(path, 'rb') as f:
        raw = f.read()
    text = raw.decode('utf-8').replace('\r\n', '\n')
    new_text = fn(text)
    if new_text == text:
        print('NO CHANGE (already patched?): ' + path)
        return
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(new_text)
    print('PATCHED ' + path)


if __name__ == '__main__':
    patch_file(BASE + r'\finetune_base_model.py', patch_base_model)
    patch_file(BASE + r'\finetune_tokenizer.py', patch_tokenizer)
    patch_file(BASE + r'\config_loader.py', patch_config_loader)
    print('PATCH DONE')
