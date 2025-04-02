import time
import torch
import torch.nn as nn
import torch.multiprocessing as multiprocessing
import numpy as np

from math import floor, ceil
from utils.misc import temp_seed
from datetime import date

today = date.today()


class Base:
    def __init__():
        pass

    def get_reset_data(self, batch_size):
        """
        We create a initialization batch to avoid the daedlocking.
        The remainder of zero arrays will be cut in the end.
        np.nan makes it easy to debug
        """
        data = dict(
            states=np.full(((batch_size, self.state_dim)), np.nan, dtype=np.float32),
            next_states=np.full(
                ((batch_size, self.state_dim)), np.nan, dtype=np.float32
            ),
            actions=np.full((batch_size, self.action_dim), np.nan, dtype=np.float32),
            rewards=np.full((batch_size, 1), np.nan, dtype=np.float32),
            terminals=np.full((batch_size, 1), np.nan, dtype=np.float32),
            logprobs=np.full((batch_size, 1), np.nan, dtype=np.float32),
            entropys=np.full((batch_size, 1), np.nan, dtype=np.float32),
        )
        return data

    def calculate_workers_and_rounds(self):
        """
        Calculate the number of workers and rounds for multiprocessing training.

        Returns:
            num_worker_per_round (list): Number of workers per round.
            num_idx_per_round (list): Number of indices per round.
            rounds (int): Total number of rounds.
        """
        # Calculate required number of workers
        total_num_workers = ceil(self.batch_size / self.min_batch_for_worker)

        if total_num_workers > self.num_cores:
            # Calculate the number of workers per round per index
            num_worker_per_round = []
            rounds = ceil(total_num_workers / self.num_cores)

            remaining_workers = total_num_workers

            for _ in range(rounds):
                workers_this_round = min(remaining_workers, self.num_cores)
                num_worker_per_round.append(workers_this_round)
                remaining_workers -= workers_this_round
        else:
            # All workers can run in a single round
            num_worker_per_round = [total_num_workers]
            rounds = 1

        return num_worker_per_round, rounds


class OnlineSampler(Base):
    def __init__(
        self,
        state_dim: tuple,
        action_dim: int,
        episode_len: int,
        batch_size: int,
        min_batch_for_worker: int = 1024,
        cpu_preserve_rate: float = 0.95,
        num_cores: int | None = None,
        verbose: bool = True,
    ) -> None:
        super(Base, self).__init__()
        """
        This computes the ""very"" appropriate parameter for the Monte-Carlo sampling
        given the number of episodes and the given number of cores the runner specified.
        ---------------------------------------------------------------------------------
        Rounds: This gives several rounds when the given sampling load exceeds the number of threads
        the task is assigned. 
        This assigned appropriate parameters assuming one worker work with 2 trajectories.
        """

        # dimensional params
        self.state_dim = state_dim
        self.action_dim = action_dim

        # sampling params
        self.episode_len = episode_len
        self.min_batch_for_worker = min_batch_for_worker
        self.thread_batch_size = self.min_batch_for_worker + 2 * self.episode_len
        self.batch_size = batch_size

        # Preprocess for multiprocessing to avoid CPU overscription and deadlock
        self.cpu_preserve_rate = cpu_preserve_rate
        self.temp_cores = floor(multiprocessing.cpu_count() * self.cpu_preserve_rate)
        self.num_cores = num_cores if num_cores is not None else self.temp_cores

        (
            num_workers_per_round,
            rounds,
        ) = self.calculate_workers_and_rounds()

        self.num_workers_per_round = num_workers_per_round
        self.total_num_worker = sum(self.num_workers_per_round)
        self.rounds = rounds

        if verbose:
            print("Sampling Parameters:")
            print(
                f"Cores (usage)/(given)   : {self.num_workers_per_round}/{self.num_cores} out of {multiprocessing.cpu_count()}"
            )
            print(f"Total number of Worker  : {self.total_num_worker}")
            print(f"Max. batch size         : {self.thread_batch_size}")

        # enforce one thread for each worker to avoid CPU overscription.
        torch.set_num_threads(1)

    def collect_samples(
        self,
        env,
        policy,
        seed: int | None = None,
        deterministic: bool = False,
    ):
        """
        All sampling and saving to the memory is done in numpy.
        return: dict() with elements in numpy
        """
        t_start = time.time()

        policy_device = policy.device
        policy.to_device(torch.device("cpu"))

        # Use persistent queue from self.manager
        if not hasattr(self, "manager"):
            self.manager = multiprocessing.Manager()
            self.queue = self.manager.Queue()

        queue = self.queue

        # Iterate over rounds
        worker_idx = 0
        for round_number in range(self.rounds):
            processes = []
            for i in range(self.num_workers_per_round[round_number]):
                if worker_idx == self.total_num_worker - 1:
                    # Main thread process
                    memory = self.collect_trajectory(
                        worker_idx,
                        None,
                        env,
                        policy,
                        seed=seed,
                        deterministic=deterministic,
                    )
                else:
                    # Sub-thread process
                    worker_args = (
                        worker_idx,
                        queue,
                        env,
                        policy,
                        seed,
                        deterministic,
                    )
                    p = multiprocessing.Process(
                        target=self.collect_trajectory, args=worker_args
                    )
                    processes.append(p)
                    p.start()

                worker_idx += 1

            # Ensure all workers finish before collecting data
            for p in processes:
                p.join()

        # Include worker memories in one list
        worker_memories = [None] * worker_idx
        while not queue.empty():
            try:
                pid, worker_memory = queue.get(timeout=2)
                worker_memories[pid] = worker_memory
            except Exception as e:
                print(f"Queue retrieval error: {e}")

        worker_memories[-1] = memory  # Add main thread memory

        memory = {}
        for worker_memory in worker_memories:
            if worker_memory is None:
                raise ValueError("worker memory shouldn't be None")

            for key in worker_memory:
                if key in memory:
                    memory[key] = np.concatenate(
                        (memory[key], worker_memory[key]), axis=0
                    )
                else:
                    memory[key] = worker_memory[key]

        # Truncate to batch size
        for k, v in memory.items():
            memory[k] = v[: self.batch_size]

        policy.to_device(policy_device)
        t_end = time.time()

        return memory, t_end - t_start

    def collect_trajectory(
        self,
        pid,
        queue,
        env,
        policy: nn.Module,
        seed: int | None = None,
        deterministic: bool = False,
    ):
        # estimate the batch size to hava a large batch
        data = self.get_reset_data(batch_size=self.thread_batch_size)  # allocate memory

        if queue is not None:
            temp_seed(seed, pid)

        current_step = 0
        while current_step < self.min_batch_for_worker:
            # env initialization
            obs, _ = env.reset(seed=seed)

            for t in range(self.episode_len):
                with torch.no_grad():
                    a, metaData = policy(obs, deterministic=deterministic)
                    a = a.cpu().numpy().squeeze(0) if a.shape[-1] > 1 else [a.item()]

                    # env stepping
                    next_obs, rew, term, trunc, infos = env.step(a)
                    done = term or trunc

                # saving the data
                data["states"][current_step + t] = obs
                data["next_states"][current_step + t] = next_obs
                data["actions"][current_step + t] = a
                data["rewards"][current_step + t] = rew
                data["terminals"][current_step + t] = done
                data["logprobs"][current_step + t] = (
                    metaData["logprobs"].detach().numpy()
                )
                data["entropys"][current_step + t] = (
                    metaData["entropy"].detach().numpy()
                )

                if done:
                    # clear log
                    current_step += t + 1
                    break

                obs = next_obs

        for k in data:
            data[k] = data[k][:current_step]
        if queue is not None:
            queue.put([pid, data])
        else:
            return data
