#!flask/bin/python

from functools import partial
from flask import Flask, jsonify, make_response, request
import numpy as np
from sklearn.svm import SVC
import json
from rdkit import Chem
from cddd.inference import InferenceServer
from mso.objectives.scoring import ScoringFunction
from mso.optimizer import MPPSOOptimizer
from gruenifai.backend.postgres.queries import get_runs_for_session, get_session_from_db, get_run_from_db, run_to_db
from gruenifai.backend.registry import models_by_name

app = Flask(__name__)

np.random.seed(1234)

@app.errorhandler(404)
def not_found(error):
    return make_response(jsonify({'error': 'Not found'}), 404)


@app.route('/')
def index():
    return "Hello, World!"

#inferenceServer = InferenceModel(gpu_mem_frac=0.3, use_gpu=True)
#inferenceServer = InferenceServer(num_servers=NUM_SERVER, port_frontend='5585', port_backend='5586')
inferenceServer = InferenceServer(port_frontend=5527, use_running=True)

"""@app.route('/scoring_functions/', methods=['GET'])
def get_models():
    all_models = [scoring_function.to_dict() for scoring_function in get_all_available()]
    return jsonify(all_models)"""


@app.route('/evaluatequery/', methods=['POST'])
def evaluate_initial_query():
    data = json.loads(request.data)
    run_id = data['run_id']
    new_document = evaluation_for_run_id(run_id)
    run_to_db(new_document, key=run_id)
    return run_id


def evaluation_for_run_id(run_id):
    run = get_run_from_db(run_id)
    session_id = run['session_id']
    session = get_session_from_db(session_id)
    runs_for_session = get_runs_for_session(session_id)
    models = run['models']
    scoring_functions = [get_scoring_function_from_dict(dictionary) for dictionary in models]

    num_particles = 1
    num_swarms = 1
    num_workers = 8

    assert run == runs_for_session[0]
    query_molecule = session['queryMolecule']
    mol_block = query_molecule
    query_sml = Chem.MolToSmiles(Chem.MolFromMolBlock(mol_block, strictParsing=False))
    optimizer = MPPSOOptimizer.from_query(query_sml, num_part=num_particles, num_swarms=num_swarms,
                                      inference_server=inferenceServer,
                                      scoring_functions=scoring_functions, num_workers=num_workers)
    output = optimizer.evaluate_query()
    swarms = [swarm.to_dict() for swarm in output]
    run['swarms'] = swarms
    return run


@app.route('/optimization/', methods=['POST'])
def run_with_db():
    data = json.loads(request.data)
    run_id = data['run_id']
    new_document = run_optimization_for_run_id(run_id)
    run_to_db(new_document, key=run_id)
    return run_id


def run_optimization_for_run_id(run_id):
    run = get_run_from_db(run_id)
    session_id = run['session_id']
    session = get_session_from_db(session_id)
    runs_for_session = get_runs_for_session(session_id)
    models = run['models']
    scoring_functions = [get_scoring_function_from_dict(dictionary) for dictionary in models]

    if session["fastMode"]:
        num_particles = 50
        num_swarms = 2
        num_workers = 8
        num_steps = 2
    else:
        num_particles = 150
        num_swarms = 16
        num_workers = 16
        num_steps = 5


    # Start LO (in case the initial query was not evaluated)
    if len(runs_for_session) == 1:
        print('start Lead Optimization')
        query_molecule = session['queryMolecule']
        mol_block = query_molecule
        query_sml = Chem.MolToSmiles(Chem.MolFromMolBlock(mol_block, strictParsing=False))
        optimizer = MPPSOOptimizer.from_query(
            init_smiles=query_sml,
            num_part=num_particles,
            num_swarms=num_swarms,
            inference_server=inferenceServer,
            scoring_functions=scoring_functions,
            num_workers=num_workers)

    else:
        # Start the LO (in case the initial query was already evalutated and therefore an entry
        # exists in the database with only one swarm containing one particle, which i the query)
        completed = runs_for_session[-2]
        if len(completed['swarms']) == 1 and len(completed['swarms'][0]['particles']) == 1:
            print('start Lead Optimization')
            query_sml = completed['swarms'][0]['particles'][0]['smiles']
            optimizer = MPPSOOptimizer.from_query(query_sml, num_part=num_particles, num_swarms=num_swarms,
                                                  inference_server=inferenceServer,
                                                  scoring_functions=scoring_functions, num_workers=num_workers)

        else:
            # Continue LO for a previously initialzed swarm with multiple particles
            print('Continue LO')
            swarm_dicts = completed['swarms']
            optimizer = MPPSOOptimizer.from_swarm_dicts(swarm_dicts, inferenceServer,
                                                        scoring_functions, num_workers=num_workers)

    output, _ = optimizer.run(num_steps=num_steps)
    swarms = [swarm.to_dict() for swarm in output]
    run['swarms'] = swarms
    return run


def get_scoring_function_from_dict(dictionary):
    def train_user_score_model(good_smiles, bad_smiles):
        smls = good_smiles + bad_smiles
        x = inferenceServer.seq_to_emb(smls)
        y = np.concatenate([np.ones(len(good_smiles)), np.zeros(len(bad_smiles))])
        clf = SVC(class_weight="balanced", probability=True).fit(x, y)

        return clf.predict_proba

    def predict_proba_wrapper(function, x):
        proba_pos = function(x)[:, 1]
        return proba_pos

    def user_score_default(x):
        return 0.5 * np.ones(len(x))

    name = dictionary['name']
    desirability = dictionary.get('desirability', None)
    weight = dictionary.get('weight', 100)
    kwargs = dictionary.get('additional_args', {})
    if name == "user score":
        description = "user score"
        is_mol_func = False
        if (kwargs["good"] == []) | (kwargs["bad"] == []):
            func = user_score_default
        else:
            func = train_user_score_model(
                good_smiles=kwargs["good"],
                bad_smiles=kwargs["bad"]
            )
            func = partial(predict_proba_wrapper, func)
    else:
        func, description, is_mol_func = models_by_name[name]
        if kwargs:
            if name == "distance score":
                target = inferenceServer.seq_to_emb(Chem.MolToSmiles(Chem.MolFromMolBlock(kwargs["query"])))
                func = partial(func, target=target)
            else:
                func = partial(func, **kwargs)
    if func:
        return ScoringFunction(
            func=func,
            name=name,
            description=description,
            weight=weight,
            desirability=desirability,
            is_mol_func=is_mol_func)


if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=8897)
