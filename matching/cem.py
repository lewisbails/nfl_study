import numpy as np
import pandas as pd
from pandas import Series
from pandas.core.frame import _from_nested_dict
from typing import Union
import matplotlib.pyplot as plt
import seaborn as sns
from tqdm import tqdm
import statsmodels.formula.api as smf
import statsmodels.api as sm
from copy import deepcopy
from itertools import combinations, product
from collections import OrderedDict
from scipy.stats import ttest_ind_from_stats
from functools import reduce


class CEM:
    ''' Coarsened Exact Matching '''

    @staticmethod
    def match(data, treatment, bins, one_to_many=True):
        ''' Return weights for data given a coursening '''
        # coarsen based on supplied bins
        data_ = CEM.coarsen(data.copy(), bins)

        # only keep stata with examples from each treatment level
        gb = list(data_.drop(treatment, axis=1).columns.values)
        matched = data_.groupby(gb).filter(lambda x: len(
            x[treatment].unique()) == len(data_[treatment].unique()))

        # weight data in surviving strata
        weights = pd.Series([0] * len(data_), index=data_.index)
        if len(matched) and one_to_many:
            weights = CEM.weight(matched, treatment, weights)
        else:
            raise NotImplementedError
            # TODO: 1:1 matching using bhattacharya for each stratum, weight is 1 for the control and its treatment pair
        return weights

    @staticmethod
    def weight(data, treatment, initial_weights=None):
        if initial_weights is None:
            initial_weights = pd.Series([0] * len(data), index=data.index)
        counts = data[treatment].value_counts()
        gb = list(data.drop(treatment, axis=1).columns.values)
        weights = data.groupby(gb)[treatment].transform(lambda x: CEM._weight_stratum(x, counts))
        return weights.add(initial_weights, fill_value=0)

    @staticmethod
    def _weight_stratum(stratum, M):
        ''' Calculate weights for regression '''
        ms = stratum.value_counts()
        T = stratum.max()  # use as "treatment"
        return pd.Series([1 if c == T else (M[c] / M[T]) * (ms[T] / ms[c]) for _, c in stratum.iteritems()])

    @staticmethod
    def _bins_gen(d):
        ''' Individual coarsening dict generator '''
        od = OrderedDict(d)
        covariate, values = od.keys(), od.values()
        cut_types = [v['cut'] for v in values]
        bins = [v['bins'] for v in values]
        for bin_ in bins:
            if not isinstance(bin_, range):
                raise TypeError('Ambiguous relax process. Please use ranges.')
        combinations = product(*bins)
        for c in combinations:
            dd = [(i, {'bins': j, 'cut': k}) for i, j, k in zip(covariate, c, cut_types)]
            yield dict(dd)

    @staticmethod
    def relax(data, treatment, bins, measure='l1', continuous=[]):
        ''' Match on several coarsenings and evaluate some imbalance measure '''
        data_ = data.copy()
        length = np.prod([len(x['bins']) for x in bins.values()])

        imb_params = CEM.get_imbalance_params(data_.drop(
            treatment, axis=1), measure, continuous)  # indep. of any coarsening

        rows = []
        for bins_i in tqdm(CEM._bins_gen(bins), total=length):
            weights = CEM.match(data_, treatment, bins_i)
            nbins = np.prod([x['bins'] for x in bins_i.values()])
            if (weights > 0).sum():
                d = data_.loc[weights > 0, :]
                if treatment in bins_i:
                    # continuous treatment binning
                    d[treatment] = CEM._cut(d[treatment], bins_i[treatment]
                                            ['cut'], bins_i[treatment]['bins'])
                score = CEM.imbalance(d, treatment, measure, **imb_params)
                vc = d[treatment].value_counts()
                row = {'imbalance': score, 'coarsening': bins_i, 'bins': nbins}
                row.update({f'treatment_{t}': c for t, c in vc.items()})
                rows.append(pd.Series(row))
            else:
                rows.append(pd.Series({'imbalance': 1, 'coarsening': bins_i, 'bins': nbins}))
        return pd.DataFrame.from_records(rows)

    @staticmethod
    def regress(data, treatment, outcome, bins, measure='l1', formula=None, drop=[], continuous=[]):
        '''Regress on 1 or more coarsenings and return a summary and imbalance measure'''
        data_ = data.copy()
        bins_ = deepcopy(bins)

        if not formula:
            formula = CEM._infer_formula(data_, outcome, drop)

        n_relax = sum(isinstance(x['bins'], range) for x in bins_.values())
        if n_relax > 1:
            raise NotImplementedError('Cant handle depth>1 regression yet.')
        elif n_relax == 1:
            # Regress at different coarsenings
            k = list(filter(lambda k: isinstance(bins_[k]['bins'], range), bins_))[0]
            v = bins_[k]['bins']
            method = bins_[k]['cut']
            rows = []
            print(f'Regressing with {len(v)} different pd.{method} binnings on "{k}"\n')
            for i in tqdm(v):
                bins_[k].update({'bins': i})
                row = CEM.regress(data_, treatment, outcome, bins_, formula=formula, continuous=continuous)
                row['n_bins'] = i
                row['var'] = k
                rows.append(row)
            frame = CemFrame.from_records(rows)
            frame.set_index('n_bins', inplace=True)
            return frame
        else:
            # weights
            weights_ = CEM.match(data_.drop(outcome, axis=1), treatment, bins_)
            # imbalance
            imb_params = CEM.get_imbalance_params(data_.drop(
                [treatment, outcome], axis=1), measure, continuous)  # indep. of any coarsening
            d = data_.drop(outcome, axis=1).loc[weights_ > 0, :]
            if treatment in bins_:
                d[treatment] = CEM._cut(d[treatment], bins_[treatment]['cut'], bins_[
                    treatment]['bins'])  # labels will be ints
            score = CEM.imbalance(d, treatment, measure, **imb_params)
            # regression
            res = CEM._regress_matched(data_, formula, weights_)
            # counts
            vc = d[treatment].value_counts()  # ints if cut_ else original values
            return pd.Series({'result': res, 'imbalance': score, 'vc': vc})

    @staticmethod
    def _regress_matched(data, formula, weights):
        glm = smf.glm(formula,
                      data=data.loc[weights > 0, :],
                      family=sm.families.Binomial(),
                      var_weights=weights[weights > 0])
        result = glm.fit(method='bfgs')
        return result

    @staticmethod
    def _infer_formula(data, dv, drop):
        iv = ' + '.join(data.drop([dv] + drop, axis=1).columns.values)
        return f'{dv} ~ {iv}'

    @staticmethod
    def _cut(col, method, bins):
        if method == 'qcut':
            return pd.qcut(col, q=bins, labels=False)
        elif method == 'cut':
            return pd.cut(col, bins=bins, labels=False)
        else:
            raise Exception(
                f'"{method}" not supported. Coarsening only possible with "cut" and "qcut".')

    @staticmethod
    def coarsen(data, bins):
        ''' Coarsen data based on schema '''
        df_coarse = data.apply(lambda x: CEM._cut(
            x, bins[x.name]['cut'], bins[x.name]['bins']) if x.name in bins else x, axis=0)
        return df_coarse

    @staticmethod
    def imbalance(data, treatment, measure='l1', **kwargs):
        ''' Evaluate histogram similarity '''
        if measure in MEASURES:
            return MEASURES[measure](data, treatment, **kwargs)
        else:
            raise NotImplementedError(f'"{measure}" not a valid measure.')

    @staticmethod
    def univariate_imbalance(data, treatment, measure='l1', bins=None, ranges=None):
        assert len(data.drop(treatment, axis=1).columns) == len(bins) == len(ranges), 'Lengths not equal.'
        if measure != 'l1':
            raise NotImplementedError('Only L1 possible at the moment.')
        marginal = {}
        for col, bin_, range_ in zip(data.drop(treatment, axis=1).columns, bins, ranges):
            cem_imbalance = CEM.imbalance(data.loc[:, [col, treatment]],
                                          treatment, bins=[bin_], ranges=[range_])
            marginal[col] = pd.Series({'imbalance': cem_imbalance, 'measure': measure,
                                       'statistic': None, 'type': None, 'min': None, 'max': None})
        return pd.DataFrame.from_dict(marginal, orient='index')

    @staticmethod
    def _L1(data, treatment, bins=None, ranges=None, retargs=False, continuous=[], H=5):

        groups = data.groupby(treatment).groups
        data_ = data.drop(treatment, axis=1).copy()

        if len(continuous):
            params = CEM.get_imbalance_params(data_, 'l1', continuous=continuous, H=H)
            bins, ranges = params['bins'], params['ranges']
        else:
            if bins is None or ranges is None:
                raise Exception('continuous parameter not supplied but neither are the bins and ranges.')

        try:
            h = {}
            for k, i in groups.items():
                h[k] = np.histogramdd(data_.loc[i, :].to_numpy(), density=False, bins=bins, range=ranges)[0]
            L1 = {}
            for pair in map(dict, combinations(h.items(), 2)):
                pair = OrderedDict(pair)
                (k_left, k_right), (h_left, h_right) = pair.keys(), pair.values()  # 2 keys 2 histograms
                L1[tuple([k_left, k_right])] = np.sum(
                    np.abs(h_left / len(groups[k_left]) - h_right / len(groups[k_right]))) / 2
        except Exception as e:
            print(e)
            print(len(bins), len(ranges), len(data_.columns), data_.columns)
            if retargs:
                return 1, (bins, ranges)
            return 1
        if len(L1) == 1:
            if retargs:
                return list(L1.values())[0], (bins, ranges)
            return list(L1.values())[0]
        if retargs:
            return L1, (bins, ranges)
        return L1

    @staticmethod
    def get_imbalance_params(data, measure, **kwargs):
        # L1 binnings are set outside of any coarsening
        if measure == 'l1':
            imb_params = CEM._bins_ranges_for_L1(data, kwargs.get('continuous', []), kwargs.get('H', 5))
        else:
            imb_params = {}
        return imb_params

    @staticmethod
    def _bins_ranges_for_L1(data, continuous, H):
        bins = [min(x.nunique(), H) if name in continuous else x.nunique()
                for name, x in data.items()]
        ranges = [(x.min(), x.max()) for _, x in data.items()]
        return {'bins': bins, 'ranges': ranges}

    @staticmethod
    def LSATT(data, treatment, outcome, weights):
        # only currently valid for dichotamous treatments

        df2 = pd.concat((data, weights.rename('weights')), axis=1)
        df2 = df2.loc[df2['weights'] > 0, :]
        res = OrderedDict()
        for i, g in df2.groupby(treatment):
            weight = g['weights'].sum()

            WSOUT = (g[outcome] * g['weights']).sum()
            wave = WSOUT / weight

            ave = g[outcome].mean()
            WSSR = (g['weights'] * (g[outcome] - ave)**2).sum()
            wstd = np.sqrt(WSSR / weight)

            res[i] = [wave, wstd, len(g)]

        return res, ttest_ind_from_stats(*list(reduce(lambda x, y: x + y, res.values())), equal_var=False)


MEASURES = {
    'l1': CEM._L1,
}


class CemFrame(pd.DataFrame):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @classmethod
    def from_records(cls, *args, **kwargs) -> "CemFrame":
        return super(CemFrame, cls).from_records(*args, **kwargs)

    @classmethod
    def from_dict(cls, *args, **kwargs) -> "CemFrame":
        return super(CemFrame, cls).from_dict(*args, **kwargs)

    def _sm_summary_to_frame(self, summary):
        pd_summary = pd.read_html(summary.tables[1].as_html())[0]
        pd_summary.iloc[0, 0] = 'covariate'
        pd_summary.columns = pd_summary.iloc[0]
        pd_summary = pd_summary[1:]
        pd_summary.set_index('covariate', inplace=True)
        return pd_summary.astype(float)

    def _get_sample_info(self, row):
        sample_info = {}
        sample_info['imbalance'] = row['imbalance']
        sample_info['observations'] = pd.read_html(row['result'].summary().tables[0].as_html())[0].iloc[0, 3]
        for i, v in row['vc'].items():
            sample_info[f'treatment_{i}'] = v
        return pd.Series(sample_info)

    def _row_to_long(self, row):
        summary = self._sm_summary_to_frame(row['result'].summary())
        summary['var'] = row['var']
        summary['n_bins'] = row.name if 'n_bins' not in row.index else row['n_bins']
        summary.set_index(['n_bins', 'var'], append=True, inplace=True)
        return summary

    def covariates(self):
        return pd.concat([self._row_to_long(row) for _, row in self.iterrows()])

    def coarsenings(self):
        return self.apply(self._get_sample_info, axis=1)

    def _lineplot(self, data, ax):
        colours = {}
        for i, g in data.groupby('covariate'):
            g.plot.line(x='n_bins', y='coef', ax=ax, label=i)
            c = plt.gca().lines[-1].get_color()
            colours[i] = c
        return ax, colours

    def _scatterplot(self, data, ax, colours=None, stars=True):
        for i, g in data.groupby('covariate'):
            g.plot.scatter(x='n_bins', y='coef', s=g['P>|z|'] * 100,
                           c=[colours[i]] if colours else None, ax=ax, label=i)
            if stars:
                for j, row in g.iterrows():
                    if row['P>|z|'] <= 0.01:
                        txt = '***'
                    elif row['P>|z|'] <= 0.05:
                        txt = '**'
                    elif row['P>|z|'] <= 0.1:
                        txt = '*'
                    else:
                        txt = ''
                    ax.text(row['n_bins'], row['coef'], txt, fontsize=16)
        return ax

    def plot(self, stars=True):
        lf = self.covariates().reset_index()
        if lf['var'].nunique() > 1:
            raise Exception('Progressive coarsening plot only available for single variable.')
        else:
            var = lf['var'].iloc[0]

        fig, ax = plt.subplots()
        r = lf.reset_index()
        r = r.loc[r['covariate'] != 'Intercept', :]

        ax, colours = self._lineplot(r, ax)
        line_leg = ax.legend(loc='upper left', title='Covariates', bbox_to_anchor=(1.05, 1))
        for line in line_leg.get_lines():
            line.set_linewidth(4.0)

        ax = self._scatterplot(r, ax, colours, stars)

        from matplotlib.lines import Line2D
        sizes = np.array([1, 5, 10, 20, 50])
        circles = [Line2D([0], [0], linewidth=0.01, marker='o', color='w', markeredgecolor='g',
                          markerfacecolor='g', markersize=np.sqrt(size)) for size in sizes]
        scatter_leg = ax.legend(circles, sizes / 100, loc='lower left',
                                title='P-values', bbox_to_anchor=(1.05, 0))
        ax.add_artist(line_leg)

        fig.set_size_inches(12, 8)
        ax.set_title('Regression coefficients for progressive coarsening.')
        ax.set_ylabel('Coefficient')
        ax.set_xlabel(f'# bins for {var} coarsening')
        return ax


def marginals(data, treatment, kde=True, hist=True, n_bins=10):
    vals = data[treatment].unique()
    flatui = ["#2ecc71", "#9b59b6", "#3498db", "#e74c3c", "#34495e"]

    for col in data.drop(treatment, axis=1).columns:
        bins = np.linspace(data[col].min(), data[col].max(), n_bins + 1) if hist else None
        try:
            for i, val in enumerate(vals):
                sns.distplot(data[data[treatment] == val][col],
                             bins=bins, label=f'{treatment}={val}', kde=kde, norm_hist=hist, hist=hist, color=flatui[i])
                plt.axvline(data[data[treatment] == val][col].mean(), color=flatui[i])
        except:
            for i, val in enumerate(vals):
                sns.distplot(data[data[treatment] == val][col],
                             bins=bins, label=f'{treatment}={val}', kde=False, norm_hist=True, hist=True, color=flatui[i])
                plt.axvline(data[data[treatment] == val][col].mean(), color=flatui[i])

        plt.title(f'{col} distributions')
        plt.legend()
        plt.show()
