import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from .plan_losses import PPC, PlanCost,get_leading_hint
from .cost_model import *
from query_representation.utils import deterministic_hash,make_dir
from query_representation.viz import *
from matplotlib.backends.backend_pdf import PdfPages
import matplotlib.pyplot as plt

import multiprocessing as mp
import random
from collections import defaultdict
import pandas as pd
import networkx as nx
import os
import wandb
import pickle

import pdb

def get_eval_fn(loss_name):
    if loss_name == "qerr":
        return QError()
    elif loss_name == "abs":
        return AbsError()
    elif loss_name == "rel":
        return RelativeError()
    elif loss_name == "ppc":
        return PostgresPlanCost(cost_model="C")
    elif loss_name == "ppc2":
        return PostgresPlanCost(cost_model="C2")
    elif loss_name == "plancost":
        return SimplePlanCost()
    elif loss_name == "flowloss":
        return FlowLoss()
    elif loss_name == "constraints":
        return LogicalConstraints()
    else:
        assert False

class EvalFunc():
    def __init__(self, **kwargs):
        pass

    def save_logs(self, qreps, errors, **kwargs):
        result_dir = kwargs["result_dir"]
        if result_dir is None:
            return

        if "samples_type" in kwargs:
            samples_type = kwargs["samples_type"]
        else:
            samples_type = ""

        resfn = os.path.join(result_dir, self.__str__() + ".csv")
        res = pd.DataFrame(data=errors, columns=["errors"])
        res["samples_type"] = samples_type
        # TODO: add other data?
        if os.path.exists(resfn):
            res.to_csv(resfn, mode="a",header=False)
        else:
            res.to_csv(resfn, header=True)

    def eval(self, qreps, preds, **kwargs):
        '''
        @qreps: [qrep_1, ...qrep_N]
        @preds: [{},...,{}]

        @ret: [qerror_1, ..., qerror_{num_subplans}]
        Each query has multiple subplans; the returned list flattens it into a
        single array. The subplans of a query are sorted alphabetically (see
        _get_all_cardinalities)
        '''
        pass

    def __str__(self):
        return self.__class__.__name__

    # TODO: stuff for saving logs

def fix_query(query):
    # these conditions were needed due to some edge cases while generating the
    # queries on the movie_info_idx table, but crashes pyscopg2 somewhere.
    # Removing them shouldn't effect the queries.
    bad_str1 = "mii2.info ~ '^(?:[1-9]\d*|0)?(?:\.\d+)?$' AND"
    bad_str2 = "mii1.info ~ '^(?:[1-9]\d*|0)?(?:\.\d+)?$' AND"
    if bad_str1 in query:
        query = query.replace(bad_str1, "")
    if bad_str2 in query:
        query = query.replace(bad_str2, "")

    return query

def _get_all_cardinalities(qreps, preds):
    ytrue = []
    yhat = []
    for i, pred_subsets in enumerate(preds):
        qrep = qreps[i]["subset_graph"].nodes()
        keys = list(pred_subsets.keys())
        keys.sort()
        for alias in keys:
            pred = pred_subsets[alias]
            actual = qrep[alias]["cardinality"]["actual"]
            if actual == 0:
                actual += 1
            ytrue.append(float(actual))
            yhat.append(float(pred))
    return np.array(ytrue), np.array(yhat)

class LogicalConstraints(EvalFunc):
    def __init__(self, **kwargs):
        pass

    def save_logs(self, qreps, errors, **kwargs):
        pass
        # result_dir = kwargs["result_dir"]
        # if result_dir is None:
            # return

        # if "samples_type" in kwargs:
            # samples_type = kwargs["samples_type"]
        # else:
            # samples_type = ""

        # resfn = os.path.join(result_dir, self.__str__() + ".csv")
        # res = pd.DataFrame(data=errors, columns=["errors"])
        # res["samples_type"] = samples_type
        # # TODO: add other data?
        # if os.path.exists(resfn):
            # res.to_csv(resfn, mode="a",header=False)
        # else:
            # res.to_csv(resfn, header=True)

    def eval(self, qreps, preds, **kwargs):
        '''
        @qreps: [qrep_1, ...qrep_N]
        @preds: [{},...,{}]

        @ret: [qerror_1, ..., qerror_{num_subplans}]
        Each query has multiple subplans; the returned list flattens it into a
        single array. The subplans of a query are sorted alphabetically (see
        _get_all_cardinalities)
        '''
        errors = []
        id_errs = []
        fkey_errs = []

        featurizer = kwargs["featurizer"]

        for qi, qrep in enumerate(qreps):
            cur_errs = []

            cur_preds = preds[qi]
            sg = qrep["subset_graph"]
            jg = qrep["join_graph"]
            for node in sg.nodes():
                if node == SOURCE_NODE:
                    continue

                edges = sg.out_edges(node)
                nodepred = cur_preds[node]
                # calculating error per node instead of per edge
                error = 0

                for edge in edges:
                    prev_node = edge[1]
                    newt = list(set(edge[0]) - set(edge[1]))[0]
                    tab_pred = cur_preds[(newt,)]
                    for alias in edge[1]:
                        if (alias,newt) in jg.edges():
                            jdata = jg.edges[(alias,newt)]
                        elif (newt,alias) in jg.edges():
                            jdata = jg.edges[(newt,alias)]
                        else:
                            continue
                        if newt not in jdata or alias not in jdata:
                            continue

                        newjkey = jdata[newt]
                        otherjkey = jdata[alias]

                        if not featurizer.feat_separate_alias:
                            newjkey = ''.join([ck for ck in newjkey if not ck.isdigit()])
                            otherjkey = ''.join([ck for ck in otherjkey if not ck.isdigit()])

                        stats1 = featurizer.join_key_stats[newjkey]
                        stats2 = featurizer.join_key_stats[otherjkey]

                        newjcol = newjkey[newjkey.find(".")+1:]
                        if newjcol == "id":
                            card1 = cur_preds[(newt,)]
                            maxfkey = stats2["max_key"]
                            maxcard1 = maxfkey*card1

                            ## FIXME: not fully accurate
                            if cur_preds[node] > maxcard1:
                                fkey_errs.append(1.0)
                            else:
                                fkey_errs.append(0.0)

                            # could not have got bigger
                            if cur_preds[prev_node] < cur_preds[node]:
                                error = 1
                                id_errs.append(1)
                            else:
                                id_errs.append(0)

                        # else:
                            # # new table was a foreign key
                            # maxfkey = stats1["max_key"]
                            # card_prev = cur_preds[prev_node]
                            # maxcurcard = card_prev * maxfkey
                            # if maxcurcard < cur_preds[node]:
                                # print("BAD")
                                # pdb.set_trace()

                cur_errs.append(error)
            errors.append(np.mean(cur_errs))

        print("pkey x fkey errors: ", np.mean(fkey_errs), np.sum(fkey_errs))
        print("primary key id errors: ", np.mean(id_errs))
        return errors

    def __str__(self):
        return self.__class__.__name__

    # TODO: stuff for saving logs

class QError(EvalFunc):
    def eval(self, qreps, preds, **kwargs):
        '''
        '''
        assert len(preds) == len(qreps)
        assert isinstance(preds[0], dict)

        ytrue, yhat = _get_all_cardinalities(qreps, preds)
        assert len(ytrue) == len(yhat)
        assert 0.00 not in ytrue
        assert 0.00 not in yhat

        errors = np.maximum((ytrue / yhat), (yhat / ytrue))

        self.save_logs(qreps, errors, **kwargs)

        return errors

class AbsError(EvalFunc):
    def eval(self, qreps, preds, **kwargs):
        '''
        '''
        assert len(preds) == len(qreps)
        assert isinstance(preds[0], dict)

        ytrue, yhat = _get_all_cardinalities(qreps, preds)
        errors = np.abs(yhat - ytrue)
        return errors

class RelativeError(EvalFunc):
    def eval(self, qreps, preds, **kwargs):
        '''
        '''
        assert len(preds) == len(qreps)
        assert isinstance(preds[0], dict)
        ytrue, yhat = _get_all_cardinalities(qreps, preds)
        # TODO: may want to choose a minimum estimate
        # epsilons = np.array([1]*len(yhat))
        # ytrue = np.maximum(ytrue, epsilons)

        errors = np.abs(ytrue - yhat) / ytrue
        return errors

class PostgresPlanCost(EvalFunc):
    def __init__(self, cost_model="C"):
        self.cost_model = cost_model

    def __str__(self):
        return self.__class__.__name__ + "-" + self.cost_model

    def save_logs(self, qreps, errors, **kwargs):
        if "result_dir" not in kwargs:
            return

        use_wandb = kwargs["use_wandb"]
        result_dir = kwargs["result_dir"]
        if result_dir is None and not use_wandb:
            return

        save_pdf_plans = kwargs["save_pdf_plans"]
        sqls = kwargs["sqls"]
        plans = kwargs["plans"]
        opt_costs = kwargs["opt_costs"]
        pg_costs = kwargs["pg_costs"]

        true_cardinalities = kwargs["true_cardinalities"]
        est_cardinalities = kwargs["est_cardinalities"]
        costs = errors

        if "samples_type" in kwargs:
            samples_type = kwargs["samples_type"]
        else:
            samples_type = ""
        if "alg_name" in kwargs:
            alg_name = kwargs["alg_name"]
        else:
            alg_name = "Est"

        if result_dir is not None:
            costs_fn = os.path.join(result_dir, self.__str__() + ".csv")

            if os.path.exists(costs_fn):
                costs_df = pd.read_csv(costs_fn)
            else:
                columns = ["qname", "join_order", "exec_sql", "cost"]
                costs_df = pd.DataFrame(columns=columns)

            cur_costs = defaultdict(list)

            for i, qrep in enumerate(qreps):
                # sql_key = str(deterministic_hash(qrep["sql"]))
                # cur_costs["sql_key"].append(sql_key)
                qname = os.path.basename(qrep["name"])
                cur_costs["qname"].append(qname)

                joinorder = get_leading_hint(qrep["join_graph"], plans[i])
                cur_costs["join_order"].append(joinorder)

                cur_costs["exec_sql"].append(sqls[i])
                cur_costs["cost"].append(costs[i])

            cur_df = pd.DataFrame(cur_costs)
            combined_df = pd.concat([costs_df, cur_df], ignore_index=True)
            combined_df.to_csv(costs_fn, index=False)

        # FIXME: hard to append to pdfs, so use samples_type to separate
        # out the different times this function is currently called.

        if save_pdf_plans:
            pdffn = samples_type + "_query_plans.pdf"
            pdf = PdfPages(os.path.join(result_dir, pdffn))
            for i, plan in enumerate(plans):
                if plan is None:
                    continue
                # we know cost of this; we know best cost;
                title_fmt = """{}. PostgreSQL Plan Cost w/ True Cardinalities: {}\n; PostgreSQL Plan Cost w/ {} Estimates: {}\n PostgreSQL Plan using {} Estimates"""

                title = title_fmt.format(qreps[i]["name"], opt_costs[i],
                        alg_name, costs[i], alg_name)

                # no idea why explains we get from cursor.fetchall() have so
                # many nested lists[][]
                plot_explain_join_order(plan[0][0][0], true_cardinalities[i],
                        est_cardinalities[i], pdf, title)

            pdf.close()

        # Total costs
        # compute total costs
        totalcost = np.sum(costs)
        opttotal = np.sum(opt_costs)

        if len(pg_costs) == 0:
            pgtotal = -1
            relcost_pg = -1
        else:
            pgtotal = np.sum(pg_costs)
            relcost_pg = np.round(float(totalcost) / pgtotal, 6)

        relcost = np.round(float(totalcost)/opttotal, 3)

        ppes = costs - opt_costs

        print("{}, {}, #samples: {}, relative_cost: {}, pg_relative_cost: {}"\
                .format(samples_type, alg_name, len(costs),
                    relcost, relcost_pg))

        template_costs = defaultdict(list)
        true_template_costs = defaultdict(list)
        tmp_rel_costs = {}
        tmp_avg_errs = {}

        for ci in range(len(costs)):
            template = qreps[ci]["template_name"]
            template_costs[template].append(costs[ci])
            true_template_costs[template].append(opt_costs[ci])

        for tmp in template_costs:
            tmp_costs = np.array(template_costs[tmp])
            tmp_opt_costs = np.array(true_template_costs[tmp])
            tmp_relc = np.round(np.sum(tmp_costs) / float(np.sum(tmp_opt_costs)), 3)
            tmp_avg_err = np.round(np.mean(tmp_costs - tmp_opt_costs), 3)
            tmp_rel_costs[tmp] = tmp_relc
            tmp_avg_errs[tmp] = tmp_avg_err
            print("Template: {}, Relative Cost: {}, Avg Err: {}".format(tmp,
                tmp_relc, tmp_avg_err))

        if self.cost_model == "C":
            suffix = ""
        else:
            suffix = "-" + self.cost_model

        if use_wandb:
            loss_key = "Final-{}-{}{}".format("Relative-TotalPPCost",
                                                   samples_type,
                                                   suffix)
            wandb.run.summary[loss_key] = relcost

            if relcost_pg != 0.0:
                loss_key = "Final-{}-{}{}".format("Relative-PG-TotalPPCost",
                                                       samples_type,
                                                       suffix)
                wandb.run.summary[loss_key] = relcost_pg

            loss_key = "Final-{}-{}{}-mean".format("PPError",
                                                samples_type,
                                                suffix)
            wandb.run.summary[loss_key] = np.mean(ppes)

            loss_key = "Final-{}-{}{}-99p".format("PPError",
                                                samples_type,
                                                suffix)
            wandb.run.summary[loss_key] = np.percentile(ppes, 99)

            for tmp, tmpcost in tmp_rel_costs.items():
                loss_key = "Final-{}-{}{}-{}".format("Relative-TotalPPCost",
                                                       samples_type,
                                                       suffix, tmp)
                wandb.run.summary[loss_key] = tmpcost

    def eval(self, qreps, preds, user="imdb",pwd="password",
            db_name="imdb", db_host="localhost", port=5432, num_processes=-1,
            result_dir=None, **kwargs):
        ''''
        @kwargs:
            cost_model: this is just a convenient key to specify the PostgreSQL
            configuration to use. You can implement new versions in the function
            set_cost_model. e.g., cm1: disable materialization and parallelism, and
            enable all other flags.
        @ret:
            pg_costs
            Further, the following are saved in the result logs
                pg_costs:
                pg_plans: explains used to get the pg costs
                pg_sqls: sqls to execute
        '''
        assert isinstance(qreps, list)
        assert isinstance(preds, list)
        assert isinstance(qreps[0], dict)
        cost_model = self.cost_model

        if num_processes == -1:
            pool = mp.Pool(int(mp.cpu_count()))
        elif num_processes == -2:
            pool = None
        else:
            pool = mp.Pool(num_processes)

        ppc = PPC(cost_model, user, pwd, db_host,
                port, db_name)

        est_cardinalities = []
        true_cardinalities = []

        sqls = []
        join_graphs = []

        pg_query_costs = {}
        pg_costs = []
        if "query_dir" in kwargs and kwargs["query_dir"] is not None:
            pgfn = os.path.join(kwargs["query_dir"],
                    "postgres-{}.pkl".format(str(self)))
            if os.path.exists(pgfn):
                with open(pgfn, "rb") as f:
                    pg_query_costs = pickle.load(f)
            else:
                pg_query_costs = {}

        for i, qrep in enumerate(qreps):
            if not "job" in qrep["template_name"]:
                continue
            # open saved scores
            if qrep["name"] in pg_query_costs:
                pg_costs.append(pg_query_costs[qrep["name"]])

        if len(pg_costs) != len(qreps):
            pg_costs = []

        for i, qrep in enumerate(qreps):
            sqls.append(qrep["sql"])
            join_graphs.append(qrep["join_graph"])
            ests = {}
            trues = {}
            predq = preds[i]
            for node, node_info in qrep["subset_graph"].nodes().items():
                if node == SOURCE_NODE:
                    continue
                est_card = predq[node]
                alias_key = ' '.join(node)
                trues[alias_key] = node_info["cardinality"]["actual"]
                # pgs[alias_key] = node_info["cardinality"]["expected"]
                if est_card == 0:
                    est_card += 1
                ests[alias_key] = est_card
            est_cardinalities.append(ests)
            true_cardinalities.append(trues)

        # some edge cases to handle to get the qreps to work in the PostgreSQL
        for i,sql in enumerate(sqls):
            sqls[i] = fix_query(sql)

        costs, opt_costs, plans, sqls = \
                    ppc.compute_costs(sqls, join_graphs,
                            true_cardinalities, est_cardinalities,
                            num_processes=num_processes,
                            pool=pool)

        self.save_logs(qreps, costs, **kwargs, sqls=sqls,
                plans=plans, opt_costs=opt_costs,
                pg_costs = pg_costs,
                true_cardinalities=true_cardinalities,
                est_cardinalities=est_cardinalities,
                result_dir=result_dir)

        if pool is not None:
            pool.close()

        return costs

class SimplePlanCost(EvalFunc):
    def eval(self, qreps, preds, cost_model="C",
            num_processes=-1, **kwargs):
        assert isinstance(qreps, list)
        assert isinstance(preds, list)
        assert isinstance(qreps[0], dict)
        use_wandb = kwargs["use_wandb"]
        if "samples_type" in kwargs:
            samples_type = kwargs["samples_type"]
        else:
            samples_type = ""

        if num_processes == -1:
            pool = mp.Pool(int(mp.cpu_count()))
        else:
            pool = mp.Pool(num_processes)

        pc = PlanCost(cost_model)
        costs, opt_costs = pc.compute_costs(qreps, preds, pool=pool)
        pool.close()

        totalcost = np.sum(costs)
        opttotal = np.sum(opt_costs)
        relcost = np.round(float(totalcost)/opttotal, 3)

        print("{}, #samples: {}, plancost relative: {}"\
                .format(samples_type, len(costs),
                    relcost))

        if use_wandb:
            loss_key = "Final-{}-{}".format("Relative-TotalSimplePlanCost",
                                                   samples_type)
            wandb.run.summary[loss_key] = relcost

        return costs
