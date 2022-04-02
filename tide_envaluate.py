# -*- coding: UTF-8 -*-
'''
@Project ：code 
@File ：tide_envaluate.py
@Author ：jzl
@Date ：2022/3/13 14:39 
'''

from tidecv import TIDE
import tidecv.datasets as datasets

gt = datasets.YTVIS2021_perimg(path='/home/jwh/vis/mini360relate/valid_mini.json')
mask_results = datasets.YTVIS2021Result_perimg(path='/home/jwh/vis/mini360relate/results_minioriginal.json',
                                               data_ann=gt)
tide = TIDE()

tide.evaluate_range(gt, mask_results, mode=TIDE.MASK)
tide.summarize()
tide.plot(out_dir='./tide_output')
