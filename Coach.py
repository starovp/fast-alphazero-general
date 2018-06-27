from SelfPlayAgent import SelfPlayAgent
import torch
from torch import multiprocessing as mp
from torch.utils.data import TensorDataset, ConcatDataset, DataLoader
from tensorboardX import SummaryWriter

from Arena import Arena
from MCTS import MCTS
from othello.OthelloPlayers import RandomPlayer, GreedyOthelloPlayer, NNPlayer
from pytorch_classification.utils import Bar, AverageMeter
from queue import Empty
import numpy as np
from time import time


class Coach:
    def __init__(self, game, nnet, args):
        self.game = game
        self.nnet = nnet
        self.pnet = self.nnet.__class__(self.game)
        self.args = args

        self.nnet.save_checkpoint(folder=self.args.checkpoint, filename=f'iteration-best.pkl')

        self.agents = []
        self.input_tensors = []
        self.policy_tensors = []
        self.value_tensors = []
        self.batch_ready = []
        self.ready_queue = mp.Queue()
        self.file_queue = mp.Queue()
        self.completed = mp.Value('i', 0)
        self.games_played = mp.Value('i', 0)
        self.writer = SummaryWriter()
        boardx, boardy = self.game.getBoardSize()

        for i in range(self.args.workers):
            self.input_tensors.append(torch.zeros([self.args.process_batch_size, boardx, boardy]))
            self.input_tensors[i].pin_memory()
            self.input_tensors[i].share_memory_()

            self.policy_tensors.append(torch.zeros([self.args.process_batch_size, self.game.getActionSize()]))
            self.policy_tensors[i].pin_memory()
            self.policy_tensors[i].share_memory_()

            self.value_tensors.append(torch.zeros([self.args.process_batch_size, 1]))
            self.value_tensors[i].pin_memory()
            self.value_tensors[i].share_memory_()

            self.batch_ready.append(mp.Event())

    def learn(self):
        for i in range(1, self.args.numIters + 1):
            print(f'------ITER {i}------')
            self.generateSelfPlayAgents()
            self.processSelfPlayBatches()
            self.saveIterationSamples(i)
            self.killSelfPlayAgents()
            self.train(i)
            self.compareToRandom(i)
            self.compareToGreedy(i)
            self.compareToBest(i)
            print()

    def generateSelfPlayAgents(self):
        self.ready_queue = mp.Queue()
        for i in range(self.args.workers):
            self.agents.append(
                SelfPlayAgent(i, self.game, self.ready_queue, self.batch_ready[i],
                              self.input_tensors[i], self.policy_tensors[i], self.value_tensors[i], self.file_queue,
                              self.completed, self.games_played, self.args))
            self.agents[i].start()

    def processSelfPlayBatches(self):
        sample_time = AverageMeter()
        bar = Bar('Generating Samples', max=self.args.gamesPerIteration)
        end = time()

        n = 0
        while self.completed.value != self.args.workers:
            try:
                id = self.ready_queue.get(timeout=1)
                self.policy, self.value = self.nnet.process(self.input_tensors[id])
                self.policy_tensors[id].copy_(self.policy, non_blocking=True)
                self.value_tensors[id].copy_(self.value, non_blocking=True)
                self.batch_ready[id].set()
            except Empty:
                pass
            size = self.games_played.value
            if size > n:
                sample_time.update((time() - end) / (size - n), size - n)
                n = size
                end = time()
            bar.suffix = f'({size}/{self.args.gamesPerIteration}) Sample Time: {sample_time.avg:.3f}s | Total: {bar.elapsed_td} | ETA: {bar.eta_td:}'
            bar.goto(size)
        bar.finish()
        print()

    def killSelfPlayAgents(self):
        for i in range(self.args.workers):
            self.agents[i].join()
            self.batch_ready[i] = mp.Event()
        self.agents = []
        self.ready_queue = mp.Queue()
        self.completed = mp.Value('i', 0)
        self.games_played = mp.Value('i', 0)

    def saveIterationSamples(self, iteration):
        boardx, boardy = self.game.getBoardSize()
        data_tensor = torch.zeros([self.file_queue.qsize(), boardx, boardy])
        policy_tensor = torch.zeros([self.file_queue.qsize(), self.game.getActionSize()])
        value_tensor = torch.zeros([self.file_queue.qsize(), 1])
        i = 0
        try:
            while i < self.file_queue.qsize():
                data, policy, value = self.file_queue.get(timeout=1)
                data_tensor[i] = torch.from_numpy(data)
                policy_tensor[i] = torch.tensor(policy)
                value_tensor[i, 0] = value
                i += 1
        except Empty:
            pass

        torch.save(data_tensor[:1], f'{self.args.data}/iteration-{iteration:04d}-data.pkl')
        torch.save(policy_tensor[:1], f'{self.args.data}/iteration-{iteration:04d}-policy.pkl')
        torch.save(value_tensor[:1], f'{self.args.data}/iteration-{iteration:04d}-value.pkl')

    def train(self, iteration):
        datasets = []
        for i in range(max(1, iteration - self.args.numItersForTrainExamplesHistory), iteration + 1):
            data_tensor = torch.load(f'{self.args.data}/iteration-{i:04d}-data.pkl')
            policy_tensor = torch.load(f'{self.args.data}/iteration-{i:04d}-policy.pkl')
            value_tensor = torch.load(f'{self.args.data}/iteration-{i:04d}-value.pkl')
            datasets.append(TensorDataset(data_tensor, policy_tensor, value_tensor))

        dataset = ConcatDataset(datasets)
        dataloader = DataLoader(dataset, batch_size=self.args.train_batch_size, shuffle=True,
                                num_workers=self.args.workers)

        l_pi, l_v = self.nnet.train(dataloader)
        self.writer.add_scalar('loss/policy', l_pi, iteration)
        self.writer.add_scalar('loss/value', l_v, iteration)
        self.writer.add_scalar('loss/total', l_pi + l_v, iteration)

        self.nnet.save_checkpoint(folder=self.args.checkpoint, filename=f'iteration-{iteration:04d}.pkl')

        del dataloader
        del dataset
        del datasets

    def compareToBest(self, iteration):
        self.pnet.load_checkpoint(folder=self.args.checkpoint, filename=f'iteration-best.pkl')
        pplayer = NNPlayer(self.game, self.pnet, self.args.arenaTemp)
        nplayer = NNPlayer(self.game, self.nnet, self.args.arenaTemp)
        print(f'PITTING AGAINST BEST VERSION')

        arena = Arena(pplayer.play, nplayer.play, self.game)
        pwins, nwins, draws = arena.playGames(self.args.arenaCompare)

        print(f'NEW/BEST WINS : %d / %d ; DRAWS : %d' % (nwins, pwins, draws))
        self.writer.add_scalar(f'win_rate/best', float(nwins) / (pwins + nwins), iteration)
        if not (pwins + nwins > 0 and float(nwins) / (pwins + nwins) < self.args.updateThreshold):
            print('ACCEPTING NEW BEST MODEL')
            self.nnet.save_checkpoint(folder=self.args.checkpoint, filename='iteration-best.pkl')

    def compareToRandom(self, iteration):
        self.pnet.load_checkpoint(folder=self.args.checkpoint, filename='iteration-best.pkl')
        r = RandomPlayer(self.game)
        nnplayer = NNPlayer(self.game, self.nnet, self.args.arenaTemp)
        print('PITTING AGAINST RANDOM')

        arena = Arena(r.play, nnplayer.play, self.game)
        pwins, nwins, draws = arena.playGames(self.args.arenaCompare)

        print('NEW/RANDOM WINS : %d / %d ; DRAWS : %d' % (nwins, pwins, draws))
        self.writer.add_scalar('win_rate/random', float(nwins) / (pwins + nwins), iteration)

    def compareToGreedy(self, iteration):
        self.pnet.load_checkpoint(folder=self.args.checkpoint, filename='iteration-best.pkl')
        g = GreedyOthelloPlayer(self.game)
        nnplayer = NNPlayer(self.game, self.nnet, self.args.arenaTemp)
        print('PITTING AGAINST GREEDY')

        arena = Arena(g.play, nnplayer.play, self.game)
        pwins, nwins, draws = arena.playGames(self.args.arenaCompare)

        print('NEW/GREEDY WINS : %d / %d ; DRAWS : %d' % (nwins, pwins, draws))
        self.writer.add_scalar('win_rate/greedy', float(nwins) / (pwins + nwins), iteration)
